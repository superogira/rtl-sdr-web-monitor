"""Digital voice decoding through an external `dsd-neo` subprocess.

WHY A SUBPROCESS AND NOT ctypes
-------------------------------
`mbelib-neo` (the vocoder library) only turns an ALREADY-SYNCHRONISED voice
frame into PCM: it takes a `char[4][24]` / `char[8][23]` frame of bits and
emits 160 samples. Everything before that point is the hard part and it is
NOT in mbelib-neo:

    IQ -> [1] C4FM/GFSK demod   -> [2] symbol & timing recovery
       -> [3] frame sync        -> [4] FEC (Golay/Hamming/Trellis/RS, interleave)
       -> [5] voice frame bits  -> vocoder -> PCM

`dsd-neo` supplies steps 1-4 and links mbelib-neo for the vocoder, so calling
it keeps this project's MIT licensing at arm's length: we ship no GPL code and
link nothing GPL at build time, we only execute a separate program over a
pipe. See docs/DIGITAL_MODES.md for the full rationale.

DATA FLOW
---------
    StreamingDemodulator._process_digital()   -> float32 @ 48 kHz
        -> DigitalDecoder.feed()              -> PCM16LE mono on stdin
        -> dsd-neo                            -> PCM16LE @ 8 kHz on stdout
        -> DigitalDecoder.read()              -> float32 @ 8 kHz

Note the rate change: the decoder always emits 8 kHz (dsd-neo's presets pin
`pulse_digi_rate_out = 8000`), NOT the SDR's AUDIO_SAMPLE_RATE. Listeners must
report that rate in the AUD2 header.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Optional

import numpy as np

from backend.audio.demod import (
    DIGITAL_AUDIO_RATE, DIGITAL_INPUT_RATE, DIGITAL_MODE_FLAGS, DemodMode,
)

logger = logging.getLogger(__name__)

# Where an AppImage extraction or a source build commonly ends up. Checked in
# order after $DSD_NEO_BIN and PATH so a plain package install just works.
_DEFAULT_PATHS = (
    "/opt/dsd-neo/squashfs-root/AppRun",
    "/opt/dsd-neo/dsd-neo.AppImage",
    "/usr/local/bin/dsd-neo",
    "/usr/bin/dsd-neo",
)

# A 20 ms voice frame at 8 kHz is 160 samples = 320 bytes (mono) / 640 (stereo).
# dsd-neo writes 160- or 960-frame blocks, so 4 KB is always a whole number of
# frames in either layout.
_READ_CHUNK = 4096

# How long stop() waits for dsd-neo to flush and exit before killing it.
_STOP_TIMEOUT = 2.0

# Output layout PER PRESET, taken from dsd-neo's own decode_mode.c:
#
#   decode_mode_apply_dmr()        -> pulse_digi_out_channels = 2  (stereo)
#   decode_mode_apply_p25p1()      -> pulse_digi_out_channels = 1  (mono)
#   decode_mode_apply_nxdn48/96()  -> 1
#   decode_mode_apply_dstar()      -> 1
#   decode_mode_apply_dpmr()       -> 1
#
# This is a CONTRACT of the preset, so it must be the source of truth for how
# we de-interleave. Deriving it from the size of a pipe read is unsound:
# asyncio's StreamReader.read() returns whatever is available, so a stereo
# block can arrive cut in half — and half of a 160-frame stereo block is
# 160 int16, which is byte-for-byte identical to a full mono block. Guessing
# from that made DMR latch to MONO, which interleaves the two TDMA slots into
# one stream and stretches the speech to 2x its real duration ("slow/dragged").
_STEREO_MODES = frozenset({DemodMode.DMR})

# Samples per dsd-neo voice block: 160 at the native 8 kHz, or 960 when its
# 6x upsampler is engaged (pulse_digi_rate_out > 8000 -> 48 kHz).
_FRAMES_NATIVE = 160
_FRAMES_UPSAMPLED = 960


def _mode_channels(mode: DemodMode) -> int:
    """Output channels dsd-neo uses for this preset's digital voice stream."""
    return 2 if mode in _STEREO_MODES else 1

# Lifecycle observers. The AudioBroadcaster registers one so it can enforce the
# global process cap (settings.DIGITAL_DECODE_MAX_PROC) and report counts,
# without decoder.py importing broadcaster.py (which imports this module).
_lifecycle_hooks: list = []


def add_lifecycle_hook(fn) -> None:
    """Register `fn(decoder, started: bool)` to observe decoder start/stop."""
    if fn not in _lifecycle_hooks:
        _lifecycle_hooks.append(fn)


def _notify(dec: "DigitalDecoder", started: bool) -> None:
    for fn in list(_lifecycle_hooks):
        try:
            fn(dec, started)
        except Exception:  # noqa: BLE001 - a hook must not break decoding
            logger.debug("decoder lifecycle hook failed", exc_info=True)


def resolve_binary(configured: str = "") -> Optional[str]:
    """Return the dsd-neo executable to run, or None when it is not installed.

    An EXPLICIT path (from DSD_NEO_BIN or the caller) is authoritative: if it
    is set but does not point at an executable file we return None rather than
    silently falling back to some other copy — a misconfigured path should be
    visible, not quietly ignored in favour of a stale build.
    """
    configured = (configured or "").strip()
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        return None
    for candidate in _DEFAULT_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("dsd-neo")


def unavailable_reason(binary: str = "") -> Optional[str]:
    """Human-readable reason digital decoding cannot run, or None if it can."""
    if resolve_binary(binary) is None:
        return ("dsd-neo is not installed. Install it or set DSD_NEO_BIN "
                "(see docs/DIGITAL_MODES.md).")
    return None


class DigitalDecoder:
    """One dsd-neo process serving one listener.

    Lifecycle is owned by the listener: started when the listener switches to a
    digital mode, stopped when it leaves one or disconnects. Instances are NOT
    shared — dsd-neo carries per-stream state (symbol timing, FEC history, the
    vocoder's previous-frame state), so one process cannot serve two different
    frequencies.
    """

    def __init__(self, mode: DemodMode, binary: str = "",
                 max_restarts: int = 5, start_timeout: float = 5.0):
        if mode not in DIGITAL_MODE_FLAGS:
            raise ValueError(f"{mode!r} is not a digital mode")
        self.mode = mode
        self.binary_configured = (binary or "").strip()
        self.binary = resolve_binary(binary)
        self.start_timeout = float(start_timeout)
        self.max_restarts = int(max_restarts)
        self.audio_rate = DIGITAL_AUDIO_RATE

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._out: "asyncio.Queue[np.ndarray]" = asyncio.Queue(maxsize=32)
        self._restarts = 0
        self._stopping = False
        self._dropped_chunks = 0
        self._feed_failures = 0
        # LOCKED at construction from the preset, never inferred from block
        # size. dsd-neo's channel count is a property of the -f preset (see
        # _STEREO_MODES); guessing it from a pipe read latches the wrong value
        # permanently and makes DMR play at 2x its duration.
        self.channels = _mode_channels(mode)
        # Bytes of an incomplete sample frame carried over to the next read.
        # StreamReader.read() may cut anywhere, including mid-int16 or between
        # the L and R halves of a stereo frame; without this the de-interleave
        # shifts by one sample and every later frame is swapped.
        self._leftover = b""
        # Sample rate of the stream we are actually producing (updated from the
        # frame count, so the 8 kHz / 48 kHz upsample case is handled).
        self._rate_known = False

    # ------------------------------------------------------------------
    # Process arguments
    # ------------------------------------------------------------------
    def _argv(self) -> list:
        """Build the dsd-neo command line.

        We keep dsd-neo's own preset output layout (rather than forcing mono
        with a flag) and pick a slot in `_decode_pcm`: DMR's two TDMA slots are
        interleaved left/right, and downmixing them would add two unrelated
        conversations together.
        """
        flag = DIGITAL_MODE_FLAGS[self.mode]
        return [
            self.binary,
            flag,
            "-i", "-",                       # raw PCM on stdin
            "-s", str(int(DIGITAL_INPUT_RATE)),
            "-o", "-",                       # decoded audio on stdout
            # The terminal UI would render to a TTY that does not exist and
            # (per dsd-neo's docs) swallow stderr diagnostics.
            "--frontend", "none",
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> bool:
        """Spawn the decoder. Returns True on success, False if unavailable."""
        if self.binary is None:
            logger.warning(
                "digital decode: dsd-neo not found (set DSD_NEO_BIN); "
                "%s will stay silent", self.mode.value,
            )
            return False
        if self.alive:
            return True

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr is piped (not DEVNULL) so a startup failure can be
                # reported instead of silently producing no audio.
                stderr=asyncio.subprocess.PIPE,
                limit=_READ_CHUNK,
            )
        except (OSError, ValueError) as exc:
            logger.error("digital decode: cannot start dsd-neo: %s", exc)
            self._proc = None
            return False

        self._stopping = False
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"dsd-{self.mode.value}")
        _notify(self, True)
        logger.info("🎧 digital decode started: %s (pid %s, %s)",
                    self.mode.value, self._proc.pid, self.binary)
        return True

    async def stop(self) -> None:
        """Flush and terminate the decoder, then reap the read task."""
        self._stopping = True
        proc, self._proc = self._proc, None

        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:  # noqa: BLE001 - closing a dead pipe is fine
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT)
            except asyncio.TimeoutError:
                # dsd-neo did not flush in time (or is blocked writing to a
                # stdout nobody reads any more) — take it down.
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

        task, self._reader_task = self._reader_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

        # Close the pipes explicitly. asyncio keeps a transport per pipe; left
        # open they surface as "unclosed transport" ResourceWarnings and leak a
        # file descriptor for every listener that restarts its decoder.
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is None:
                    continue
                try:
                    if not pipe.is_closing():
                        pipe.close()
                except Exception:  # noqa: BLE001
                    pass

        # Drop anything still queued so a restart cannot replay stale audio.
        while not self._out.empty():
            try:
                self._out.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Drop the partial frame too: it belongs to the dead process's stream,
        # and prepending it to the restarted one would shift the de-interleave.
        self._leftover = b""
        self._rate_known = False
        _notify(self, False)

    async def restart(self) -> bool:
        """Restart after an unexpected exit. False when the budget is spent."""
        if self.max_restarts > 0 and self._restarts >= self.max_restarts:
            logger.error(
                "digital decode: %s died %d times; giving up until the "
                "listener changes mode", self.mode.value, self._restarts,
            )
            return False
        self._restarts += 1
        delay = min(2.0 ** (self._restarts - 1), 8.0)
        logger.warning(
            "digital decode: restarting %s in %.1fs (attempt %d/%s)",
            self.mode.value, delay, self._restarts,
            self.max_restarts if self.max_restarts > 0 else "∞",
        )
        await asyncio.sleep(delay)
        await self.stop()
        return await self.start()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ------------------------------------------------------------------
    # Audio I/O
    # ------------------------------------------------------------------
    async def feed(self, audio: np.ndarray) -> None:
        """Write one discriminator chunk (float32 @ 48 kHz) to dsd-neo.

        Backpressure policy: never await forever. If the pipe is full the
        decoder is behind real time, and blocking here would stall the SDR
        stream loop for every other listener too. Dropping the newest chunk
        keeps what the decoder already has contiguous, which damages a burst
        far less than an inserted gap.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        if proc.stdin is None or proc.stdin.is_closing() or len(audio) == 0:
            return

        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        try:
            proc.stdin.write(pcm.tobytes())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # dsd-neo exited. The read task sees the same EOF and drives the
            # restart policy; nothing useful to do here.
            self._feed_failures += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._feed_failures += 1
            logger.debug("digital decode: feed failed: %s", exc)

    def read(self) -> np.ndarray:
        """Return all decoded audio available now (float32 @ 8 kHz), or empty.

        Non-blocking on purpose: the listener worker must be able to fall
        through to batching/squelch even when the decoder is between
        transmissions. dsd-neo emits nothing until a burst passes its FEC, so
        an empty result is the normal idle state, not an error.
        """
        parts = []
        while True:
            try:
                parts.append(self._out.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not parts:
            return np.zeros(0, dtype=np.float32)
        if len(parts) == 1:
            return parts[0]
        return np.concatenate(parts)

    async def _read_loop(self) -> None:
        """Pump dsd-neo stdout into the output queue until it exits."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                data = await proc.stdout.read(_READ_CHUNK)
                if not data:
                    break
                arr = self._decode_pcm(data)
                if arr is None or len(arr) == 0:
                    continue
                try:
                    self._out.put_nowait(arr)
                except asyncio.QueueFull:
                    # The consumer is slower than the decoder. Drop the OLDEST
                    # block: keeping the newest keeps playback near real time.
                    self._dropped_chunks += 1
                    try:
                        self._out.get_nowait()
                        self._out.put_nowait(arr)
                    except asyncio.QueueEmpty:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("digital decode: read loop ended: %s", exc)
        finally:
            if not self._stopping:
                await self._report_exit(proc)

    async def _report_exit(self, proc: asyncio.subprocess.Process) -> None:
        """Log dsd-neo's own last words once, then let the caller restart.

        dsd-neo names the real problem on stderr (bad flag, missing library,
        TETRA on a PCM input) far better than a generic "no audio" ever could.
        """
        err = b""
        try:
            if proc.stderr is not None:
                err = await asyncio.wait_for(proc.stderr.read(2000), timeout=0.5)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001
            pass
        rc = proc.returncode
        if err:
            logger.warning(
                "digital decode: %s exited (rc=%s): %s",
                self.mode.value, rc,
                err.decode("utf-8", "replace").strip()[:500],
            )
        else:
            logger.warning("digital decode: %s exited (rc=%s)",
                           self.mode.value, rc)

    def _decode_pcm(self, data: bytes) -> Optional[np.ndarray]:
        """Bytes from dsd-neo -> mono float32.

        Layout is decided by the PRESET (`self.channels`, see _STEREO_MODES),
        never by the size of this read. dsd-neo writes whole sample frames, but
        the pipe does not preserve them: a read can end between the two int16
        of one sample, or between the L and R of a stereo frame. So:

          1. prepend whatever was left over from the previous read,
          2. hand on only WHOLE frames,
          3. keep the remainder for the next call.

        Skipping step 3 was the DMR bug: half a stereo block (160 int16) is
        indistinguishable from a full mono block, so DMR latched to mono and the
        two TDMA slots were interleaved into one stream — speech stretched 2x.

        `self.audio_rate` is derived from the frame count: 160 frames is the
        native 8 kHz block, 960 is the 6x upsampled 48 kHz one.
        """
        frame_bytes = self.channels * 2      # int16 per channel
        buf = self._leftover + data if self._leftover else data
        usable = len(buf) - (len(buf) % frame_bytes)
        if usable <= 0:
            # Not even one whole frame yet — keep every byte.
            self._leftover = buf
            return None
        self._leftover = buf[usable:]

        pcm = np.frombuffer(buf[:usable], dtype="<i2").astype(np.float32)

        if self.channels == 2:
            # Keep the LEFT slot: summing L+R would add two unrelated
            # conversations together (a per-slot selector is a future feature).
            frames = pcm.reshape(-1, 2)[:, 0]
        else:
            frames = pcm

        # Rate follows the block size dsd-neo produced. Only trust a count that
        # matches one whole block; a merged read tells us nothing.
        self._update_rate(len(frames))

        return frames / 32768.0

    def _update_rate(self, n_frames: int) -> None:
        """Set audio_rate from the frames in this block (8 kHz vs 48 kHz).

        dsd-neo emits 160-frame blocks at its native 8 kHz, and 960-frame
        blocks once its 6x upsampler is engaged (48 kHz). A read can merge
        several blocks, so accept any multiple of either size; the largest size
        that divides evenly wins, because 160 also divides 960.
        """
        if n_frames <= 0:
            return
        if n_frames % _FRAMES_UPSAMPLED == 0:
            rate = 48000
        elif n_frames % _FRAMES_NATIVE == 0:
            rate = 8000
        else:
            # A partial block (we already trim to whole frames, so this only
            # happens if dsd-neo changed size mid-stream): leave the rate.
            return
        if rate != self.audio_rate:
            if self._rate_known:
                logger.warning(
                    "digital decode: %s output rate changed %d -> %d Hz",
                    self.mode.value, self.audio_rate, rate,
                )
            self.audio_rate = rate
            self._rate_known = True

    def health(self) -> dict:
        """Normalised decoder state for the UI / SIG frame.

        Distinguishes the three ways a digital listener can be silent:
          "ok"       — running; silence just means no burst passed FEC yet
          "missing"  — the executable was never found (install it, or fix
                       DSD_NEO_BIN); this will NOT fix itself
          "stopped"  — it started before and died (log has dsd-neo's reason)
        """
        if self.binary is None:
            if self.binary_configured:
                reason = (f"dsd-neo not executable at {self.binary_configured}")
            else:
                reason = "dsd-neo not installed"
            return {"state": "missing", "reason": reason,
                    "channels": None, "restarts": self._restarts,
                    "dropped": self._dropped_chunks}
        if not self.alive:
            return {"state": "stopped",
                    "reason": "decoder stopped (see server log)",
                    "channels": self.channels, "restarts": self._restarts,
                    "dropped": self._dropped_chunks}
        return {"state": "ok", "channels": self.channels,
                "restarts": self._restarts, "dropped": self._dropped_chunks}

    # ------------------------------------------------------------------
    # Introspection (status/telemetry)
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "mode": self.mode.value,
            "alive": self.alive,
            "channels": self.channels,
            "restarts": self._restarts,
            "dropped_blocks": self._dropped_chunks,
            "feed_failures": self._feed_failures,
            "audio_rate": self.audio_rate,
        }
