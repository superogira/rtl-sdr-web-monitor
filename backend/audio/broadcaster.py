"""
Audio Broadcaster - Live audio streaming to multiple WebSocket clients.

OpenWebRX-style MULTI-LISTENER architecture
--------------------------------------------
  - ONE RTL-SDR / IQ capture (shared by everyone) — fed in via feed_iq().
  - ONE demodulator PER listener, so each user can listen to a DIFFERENT
    frequency (VFO offset) / mode / bandwidth inside the captured passband
    (center frequency ± sample_rate/2) at the same time.
  - Each listener owns its own asyncio.Queue; the /ws/audio endpoint drains
    that queue and sends the PCM frames to that single WebSocket.

Concurrency / CPU
-----------------
Demodulation (scipy filters) is CPU-heavy. To avoid blocking the event loop
when several users listen at once, each listener's chunk is demodulated in a
dedicated worker thread (ThreadPoolExecutor). scipy/numpy release the GIL
during their C calls, so this yields real parallelism across listeners.
asyncio.Queue operations (put_nowait) are kept on the event-loop thread
because asyncio.Queue is not thread-safe.

NON-BLOCKING feed_iq (important for low stutter)
------------------------------------------------
feed_iq() does NOT demodulate inline. It only enqueues the raw IQ packet into
each listener's bounded iq_queue and returns immediately. A per-listener
background asyncio task (_listener_worker) drains that iq_queue, demodulates
in a worker thread, batches, and pushes AUD1 frames to the listener's output
queue (drained by the /ws/audio endpoint).

This means a slow listener (e.g. WBFM 200 kHz on a busy CPU) can no longer
stall the global SDR read loop or starve other listeners — only its own
iq_queue will drop. Previously feed_iq() did `await asyncio.gather(...)` of
ALL listeners inline, blocking the stream loop and causing stutter/freeze.

Global-audio reuse
------------------
The SDRManager already demodulates the "global" channel (center freq + global
VFO/mode/bw) for recording & signal level. When a listener listens to exactly
that same channel, we reuse the already-computed audio instead of demodulating
a second time, cutting CPU load roughly in half in the common case where most
users listen to the default/center channel.
"""
import asyncio
import concurrent.futures
import logging
import struct
import time
import uuid
from typing import Dict, Optional

import numpy as np

from backend.audio.demod import DemodMode, StreamingDemodulator

logger = logging.getLogger(__name__)

# Target batch duration in seconds. Each listener accumulates its demodulated
# audio until this much audio time is collected, then packs it into a single
# WebSocket frame. Larger = fewer frames (less overhead) but more latency.
_BATCH_DURATION = 0.10  # 100ms
# Safety cap on the number of raw chunks merged into one frame.
_MAX_CHUNKS_PER_FRAME = 10
# Hard ceiling for the worker thread pool. This is NOT the user-facing limit —
# it only bounds the ThreadPoolExecutor size so threads are created on-demand
# (not all 128 at once). The actual listener cap is configurable via the
# `max_listeners` attribute (set by admin through the settings page).
_POOL_CEILING = 128
# Absolute minimum and maximum for the configurable max_listeners value.
_MIN_LISTENERS = 1
_HARD_CAP_LISTENERS = 128
# Bounded IQ input queue per listener. Small on purpose: real-time audio cares
# about the LATEST samples, so when a listener falls behind we drop the oldest
# IQ packet rather than queueing seconds of stale audio.
_IQ_QUEUE_MAX = 4

# G.711 µ-law segment upper ends for the biased magnitude (ITU-T G.711). Used
# by pcm16_to_ulaw() to compress int16 PCM → 8-bit µ-law (half the bandwidth
# of raw int16 PCM at the same sample rate).
_ULAW_SEG_UEND = np.array(
    [0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF], dtype=np.int32
)


def pcm16_to_ulaw(samples: np.ndarray) -> np.ndarray:
    """Encode int16 PCM to G.711 µ-law bytes (uint8).

    Fully vectorized, standard-compliant (matches audioop.lin2ulaw and the
    canonical Sun/ITU reference). Output is half the size of the int16 input.
    """
    BIAS = 0x84
    CLIP = 32635
    s = samples.astype(np.int32)
    mask = np.where(s < 0, 0x7F, 0xFF)
    s = np.minimum(np.abs(s), CLIP) + BIAS
    # Segment = number of segment boundaries strictly below the biased value.
    seg = np.clip((s[:, None] > _ULAW_SEG_UEND[None, :]).sum(axis=1), 0, 7)
    mant = (s >> (seg + 3)) & 0x0F
    uval = (seg << 4) | mant
    return (uval ^ mask).astype(np.uint8)


class Listener:
    """One connected audio client.

    Owns its own StreamingDemodulator plus VFO offset / mode / channel
    bandwidth, so it can listen to any channel inside the captured passband
    INDEPENDENTLY of every other listener (OpenWebRX-style per-user VFO).

    Manual recording is ALSO per-listener: each user can record their OWN
    audio (the demodulated audio they hear, at their VFO/mode/bandwidth)
    independently of everyone else.
    """

    def __init__(self, sample_rate: int, audio_rate: int,
                 mode, channel_bw: int,
                 vfo_offset_hz: float = 0.0,
                 user_id=None, user_role=None):
        self.id = str(uuid.uuid4())
        # Output queue: AUD1 frames ready to send to the WebSocket client.
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=200)
        # Input queue: raw IQ packets waiting to be demodulated by this
        # listener's background worker. Bounded + drop-oldest so a slow
        # listener cannot accumulate stale IQ and flood the client.
        self.iq_queue: "asyncio.Queue" = asyncio.Queue(maxsize=_IQ_QUEUE_MAX)
        # Background worker task that demodulates this listener's IQ.
        self.task: Optional[asyncio.Task] = None
        self.demod = StreamingDemodulator()
        self.sample_rate = int(sample_rate)
        self.audio_rate = int(audio_rate)
        self.mode = mode if isinstance(mode, DemodMode) else DemodMode(mode)
        self.channel_bw = int(channel_bw)
        self.vfo_offset_hz = float(vfo_offset_hz)
        # Auth context (for per-user manual recording)
        self.user_id = user_id      # str UUID or None
        self.user_role = user_role  # "admin" / "user" / None
        # Per-listener audio batching (~100ms frames)
        self._batch_buf: list = []
        self._batch_samples = 0
        self._batch_sample_rate = int(audio_rate)
        # Per-listener manual recording state
        self.is_recording = False
        self.record_buffer: list = []  # list of Float32Array demod audio chunks
        self.record_start_time = None  # time.monotonic() when recording started
        # stats
        self.frames_sent = 0
        self.created_at = time.time()
        # Drop diagnostics (incremented under event-loop thread only)
        self.iq_drops = 0       # IQ packets dropped on input (slow listener)
        self.queue_drops = 0    # AUD1 frames dropped on output (slow subscriber)

    def set_vfo_offset(self, offset_hz: float) -> None:
        """Set this listener's VFO offset (Hz) relative to the center freq."""
        self.vfo_offset_hz = float(offset_hz)
        # demod.set_vfo_offset is a no-op when unchanged; cheap to call here.
        self.demod.set_vfo_offset(self.vfo_offset_hz)

    def set_mode(self, mode) -> None:
        m = mode if isinstance(mode, DemodMode) else DemodMode(mode)
        self.mode = m
        # StreamingDemodulator auto-resets its filter state on mode change
        # inside process(), so no explicit reset is needed here.

    def set_bandwidth(self, bw_hz: int) -> None:
        self.channel_bw = int(bw_hz)


class AudioBroadcaster:
    """Manages live, INDEPENDENT audio streams to multiple WebSocket clients.

    IQ flows: SDRManager._stream_loop → feed_iq() (non-blocking enqueue)
      → per-listener iq_queue → _listener_worker (per listener)
        → run_in_executor(_demod_listener_raw)  [CPU-heavy, parallel]
        → _batch_listener → AUD1 frame
      → per-listener output queue → /ws/audio endpoint task
        → websocket.send_bytes()
    """

    def __init__(self):
        # id(listener) → Listener. Keyed by id(listener) so the endpoint can
        # store the returned Listener object and unsubscribe with it directly.
        self._listeners: Dict[int, Listener] = {}
        self._lock = asyncio.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_POOL_CEILING, thread_name_prefix="demod"
        )
        # VFO passband margin ratio (kept in sync by SDRManager from app
        # settings). Listener offsets are clamped to ±sample_rate * ratio.
        self.vfo_margin_ratio = 0.40
        # Configurable maximum simultaneous listeners (set by admin via
        # settings page). Defaults to a conservative 8; range 1–128.
        self.max_listeners = 8
        # global stats
        self._chunk_count = 0
        self._bytes_sent = 0
        self._last_audio_time = 0.0

    def set_max_listeners(self, n: int) -> int:
        """Set the configurable listener cap.

        Clamps to [_MIN_LISTENERS, _HARD_CAP_LISTENERS] and returns the
        clamped value so the caller can confirm what was actually set.
        """
        n = int(n)
        if n < _MIN_LISTENERS:
            n = _MIN_LISTENERS
        elif n > _HARD_CAP_LISTENERS:
            n = _HARD_CAP_LISTENERS
        self.max_listeners = n
        return n

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    @property
    def has_listeners(self) -> bool:
        return len(self._listeners) > 0

    async def subscribe(self, sample_rate: int, audio_rate: int,
                        mode, channel_bw: int,
                        vfo_offset_hz: float = 0.0,
                        user_id=None, user_role=None) -> Listener:
        """Register a new listener and return it.

        The caller (/ws/audio endpoint) owns the returned Listener and drains
        `listener.queue` with `listener.queue.get()`.

        A per-listener background worker task is started to demodulate IQ
        off the event-loop hot path (non-blocking feed_iq).
        """
        if len(self._listeners) >= self.max_listeners:
            logger.warning(
                "🔊 Listener cap reached (%d/%d); rejecting new audio connection",
                len(self._listeners), self.max_listeners,
            )
            raise RuntimeError("max_listeners_reached")
        listener = Listener(
            sample_rate, audio_rate, mode, channel_bw, vfo_offset_hz,
            user_id=user_id, user_role=user_role,
        )
        async with self._lock:
            self._listeners[id(listener)] = listener
            # Start the per-listener demod worker (non-blocking pipeline).
            listener.task = asyncio.create_task(
                self._listener_worker(listener),
                name=f"demod-{listener.id[:8]}",
            )
        logger.info("🔊 Audio listener added (total: %d)", len(self._listeners))
        return listener

    async def unsubscribe(self, listener: Listener) -> None:
        """Remove a listener and stop its background worker."""
        async with self._lock:
            existed = self._listeners.pop(id(listener), None)
        if existed is None:
            return
        task = listener.task
        listener.task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("🔊 Audio listener removed (total: %d)", len(self._listeners))

    def reset_all_vfo(self) -> None:
        """Reset every listener's VFO offset to 0 (center frequency).

        Called by SDRManager when the hardware center frequency changes,
        because offsets are relative to the (old) center and would now point
        elsewhere on the band.
        """
        for ln in self._listeners.values():
            ln.set_vfo_offset(0.0)

    def feed_iq(self, iq_bytes: bytes, sample_rate: int,
                audio_rate: int,
                global_audio: Optional[np.ndarray] = None,
                g_mode: Optional[DemodMode] = None,
                g_channel_bw: Optional[int] = None,
                g_vfo_offset_hz: Optional[float] = None) -> None:
        """Enqueue one IQ chunk to every listener's worker (NON-BLOCKING).

        Synchronous on purpose: it only does put_nowait() on each listener's
        asyncio.Queue (which is safe to call from the event-loop thread) and
        returns immediately. Demodulation happens later in each listener's
        background worker. This keeps the SDR read loop unblocked.

        Each listener's background task picks the packet up and demodulates
        independently. If a listener's iq_queue is full (slow consumer), the
        OLDEST packet is dropped so the listener stays close to real time
        (drop counters are incremented for diag).

        `global_audio` (optional) is the audio already demodulated by
        SDRManager for the global channel (mode=g_mode, channel_bw=
        g_channel_bw, vfo=g_vfo_offset_hz). Listeners whose params match the
        global channel reuse this audio and skip their own demodulation,
        halving CPU in the common case.
        """
        if not self._listeners:
            return
        self._chunk_count += 1
        pkt = (iq_bytes, int(sample_rate), int(audio_rate),
               global_audio, g_mode, g_channel_bw, g_vfo_offset_hz)
        for ln in list(self._listeners.values()):
            try:
                ln.iq_queue.put_nowait(pkt)
            except asyncio.QueueFull:
                ln.iq_drops += 1
                # Drop the oldest IQ to keep this listener near real time.
                try:
                    ln.iq_queue.get_nowait()
                except Exception:
                    pass
                try:
                    ln.iq_queue.put_nowait(pkt)
                except Exception:
                    # Still full: packet is lost; worker will keep draining.
                    pass

    async def _listener_worker(self, ln: Listener) -> None:
        """Per-listener background task: drain iq_queue → demod → AUD1 frame.

        Runs on the event-loop thread but offloads the CPU-heavy demod step to
        a worker thread via run_in_executor (numpy/scipy release the GIL).
        Batching/encoding (cheap) runs inline. Output frames are pushed to the
        listener's output queue (drained by the /ws/audio endpoint).
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                pkt = await ln.iq_queue.get()
                (iq_bytes, sample_rate, audio_rate,
                 global_audio, g_mode, g_channel_bw, g_vfo) = pkt

                # Fast path: this listener listens to the SAME channel as the
                # global demod → reuse the already-computed audio and skip the
                # expensive per-listener demodulation.
                if self._matches_global(
                    ln, sample_rate, audio_rate,
                    global_audio, g_mode, g_channel_bw, g_vfo,
                ):
                    audio = global_audio
                else:
                    try:
                        audio = await loop.run_in_executor(
                            self._executor, self._demod_listener_raw,
                            ln, iq_bytes, sample_rate, audio_rate,
                        )
                    except Exception as e:
                        logger.warning(
                            "🔊 Listener %s demod error: %s", ln.id[:8], e
                        )
                        continue

                frame = self._batch_listener(ln, audio, audio_rate)
                if frame:
                    self._enqueue_frame(ln, frame)
        except asyncio.CancelledError:
            # Normal teardown on unsubscribe / disconnect.
            return
        except Exception as e:
            logger.error("🔊 Listener %s worker crashed: %s", ln.id[:8], e,
                         exc_info=True)

    @staticmethod
    def _matches_global(ln: Listener, sample_rate: int, audio_rate: int,
                        global_audio, g_mode, g_channel_bw, g_vfo) -> bool:
        """True if the listener can reuse the global demod audio."""
        if global_audio is None or len(global_audio) == 0:
            return False
        if g_mode is None or ln.mode != g_mode:
            return False
        if ln.sample_rate != sample_rate or ln.audio_rate != audio_rate:
            return False
        if ln.channel_bw != g_channel_bw:
            return False
        if g_vfo is None or abs(ln.vfo_offset_hz - float(g_vfo)) > 1.0:
            return False
        return True

    def _demod_listener_raw(self, listener: Listener, iq_bytes: bytes,
                            sample_rate: int, audio_rate: int) -> np.ndarray:
        """Demodulate one IQ chunk for a listener (called in a worker thread).

        Returns a float32 audio ndarray (possibly empty). Pure CPU work — no
        asyncio/queue ops here. Batching/encoding is done separately on the
        loop thread because it is cheap and touches listener state.
        """
        # Track global sample-rate changes (admin changed SDR) and clamp the
        # offset into the valid passband so the shift never aliases.
        if listener.sample_rate != sample_rate or listener.audio_rate != audio_rate:
            listener.sample_rate = int(sample_rate)
            listener.audio_rate = int(audio_rate)
            max_off = sample_rate * self.vfo_margin_ratio
            if abs(listener.vfo_offset_hz) > max_off:
                listener.vfo_offset_hz = max(-max_off, min(max_off, listener.vfo_offset_hz))

        listener.demod.set_vfo_offset(listener.vfo_offset_hz)
        audio = listener.demod.process(
            iq_bytes, listener.mode, sample_rate, audio_rate,
            channel_bw=listener.channel_bw,
        )
        return audio

    def _batch_listener(self, listener: Listener, audio: np.ndarray,
                        audio_rate: int):
        """Accumulate audio into ~100ms AUD2 frames. Runs on loop thread.

        Returns a binary AUD2 frame (bytes) ready to send, or None if the
        batch isn't full yet.

        Frame format (AUD2, G.711 µ-law):
            magic "AUD2" (4B) + sample_rate (u32 LE) + num_samples (u32 LE)
            + num_samples bytes of µ-law (1 byte/sample, half the bandwidth
            of the legacy AUD1 int16 PCM frames).
        """
        if audio is None or len(audio) == 0:
            return None

        listener._batch_buf.append(audio)
        listener._batch_samples += len(audio)
        listener._batch_sample_rate = int(audio_rate)

        batch_seconds = listener._batch_samples / max(1, listener._batch_sample_rate)
        if batch_seconds < _BATCH_DURATION and len(listener._batch_buf) < _MAX_CHUNKS_PER_FRAME:
            return None

        merged = np.concatenate(listener._batch_buf)
        listener._batch_buf.clear()
        listener._batch_samples = 0

        # Capture audio for manual recording (per-listener, full-quality PCM)
        if listener.is_recording:
            listener.record_buffer.append(merged.copy())

        # float32 [-1,1] → int16 PCM → G.711 µ-law (1 byte/sample)
        pcm = np.clip(merged, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        ulaw = pcm16_to_ulaw(pcm)
        # Frame header: magic "AUD2" + sample_rate (u32 LE) + num_samples (u32 LE)
        header = struct.pack("<4sII", b"AUD2", int(listener._batch_sample_rate), len(ulaw))
        return header + ulaw.tobytes()

    def _enqueue_frame(self, ln: Listener, frame: bytes) -> None:
        """Push a finished AUD1 frame to the listener's output queue.

        Drop-oldest on overflow so a slow WebSocket subscriber cannot
        accumulate seconds of stale audio (which would otherwise manifest as
        ever-growing latency — the "ฟลุด/ต้องกดฟังสดใหม่" symptom).
        """
        ln.frames_sent += 1
        self._bytes_sent += len(frame)
        self._last_audio_time = time.time()
        try:
            ln.queue.put_nowait(frame)
        except asyncio.QueueFull:
            ln.queue_drops += 1
            try:
                ln.queue.get_nowait()
            except Exception:
                pass
            try:
                ln.queue.put_nowait(frame)
            except Exception:
                ln.queue_drops += 1

    def get_stats(self) -> dict:
        """Return broadcaster statistics."""
        total_iq_drops = 0
        total_queue_drops = 0
        for ln in self._listeners.values():
            total_iq_drops += ln.iq_drops
            total_queue_drops += ln.queue_drops
        return {
            "listeners": len(self._listeners),
            "chunks_sent": self._chunk_count,
            "bytes_sent": self._bytes_sent,
            "iq_drops": total_iq_drops,
            "queue_drops": total_queue_drops,
            "last_audio_ago": (
                round(time.time() - self._last_audio_time, 2)
                if self._last_audio_time
                else None
            ),
        }

    def start_listener_recording(self, listener: Listener) -> bool:
        """Start manual recording for THIS listener only.

        Returns True if started, False if already recording.
        """
        if listener.is_recording:
            return False
        listener.is_recording = True
        listener.record_buffer = []
        listener.record_start_time = time.monotonic()
        logger.info(
            "🎙️ Listener %s manual recording started (user=%s)",
            listener.id[:8], listener.user_id,
        )
        return True

    def stop_listener_recording(self, listener: Listener):
        """Stop manual recording for THIS listener and return captured audio.

        Returns dict with audio data + metadata, or None if nothing captured.
        """
        if not listener.is_recording:
            return None
        listener.is_recording = False
        if not listener.record_buffer:
            listener.record_start_time = None
            return None

        audio_data = np.concatenate(listener.record_buffer)
        duration = len(audio_data) / max(1, listener.audio_rate)

        result = {
            "audio_data": audio_data,
            "duration": duration,
            "sample_rate": listener.audio_rate,
            "mode": listener.mode.value,
            "vfo_offset_hz": listener.vfo_offset_hz,
            "user_id": listener.user_id,
            "user_role": listener.user_role,
        }

        # Reset state
        listener.record_buffer = []
        listener.record_start_time = None
        logger.info(
            "🎙️ Listener %s manual recording stopped (%.1fs, user=%s)",
            listener.id[:8], duration, listener.user_id,
        )
        return result


# Global broadcaster instance
audio_broadcaster = AudioBroadcaster()