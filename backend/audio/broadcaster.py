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

This is the key difference from the old "single global demodulator + broadcast
the SAME audio to everyone" model: one user tuning no longer moves everyone
else's listening frequency. This is exactly how OpenWebRX lets many users
listen to many frequencies from one SDR.

Concurrency / CPU
-----------------
Demodulation (scipy filters) is CPU-heavy. To avoid blocking the event loop
when several users listen at once, each listener's chunk is demodulated in a
dedicated worker thread (ThreadPoolExecutor). scipy/numpy release the GIL
during their C calls, so this yields real parallelism across listeners.
asyncio.Queue operations (put_nowait) are kept on the event-loop thread
because asyncio.Queue is not thread-safe.
"""
import asyncio
import concurrent.futures
import logging
import struct
import time
import uuid
from typing import Dict

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
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=200)
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

    IQ flows: SDRManager._stream_loop → feed_iq() → per-listener demod (thread)
    → per-listener queue → /ws/audio endpoint task → websocket.send_bytes()
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
        logger.info("🔊 Audio listener added (total: %d)", len(self._listeners))
        return listener

    async def unsubscribe(self, listener: Listener) -> None:
        """Remove a listener."""
        async with self._lock:
            self._listeners.pop(id(listener), None)
        logger.info("🔊 Audio listener removed (total: %d)", len(self._listeners))

    def reset_all_vfo(self) -> None:
        """Reset every listener's VFO offset to 0 (center frequency).

        Called by SDRManager when the hardware center frequency changes,
        because offsets are relative to the (old) center and would now point
        elsewhere on the band.
        """
        for ln in self._listeners.values():
            ln.set_vfo_offset(0.0)

    async def feed_iq(self, iq_bytes: bytes, sample_rate: int,
                      audio_rate: int) -> None:
        """Demodulate one IQ chunk INDEPENDENTLY for every listener.

        Each listener is demodulated in a worker thread (parallel; GIL-friendly
        via numpy/scipy). The resulting PCM frames are pushed to each
        listener's queue on the event-loop thread.
        """
        if not self._listeners:
            return
        listeners = list(self._listeners.values())
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(
                self._executor, self._demod_listener, ln, iq_bytes,
                sample_rate, audio_rate,
            )
            for ln in listeners
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        self._chunk_count += 1
        for ln, frame in zip(listeners, results):
            if isinstance(frame, Exception):
                logger.warning("🔊 Listener %s demod error: %s", ln.id[:8], frame)
                continue
            if not frame:
                continue
            ln.frames_sent += 1
            self._bytes_sent += len(frame)
            self._last_audio_time = time.time()
            try:
                ln.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Slow subscriber: drop the oldest frame to make room.
                try:
                    ln.queue.get_nowait()
                except Exception:
                    pass
                try:
                    ln.queue.put_nowait(frame)
                except Exception:
                    pass

    def _demod_listener(self, listener: Listener, iq_bytes: bytes,
                        sample_rate: int, audio_rate: int):
        """Run one listener's demod + batching (called in a worker thread).

        Returns a binary AUD1 frame (bytes) ready to send, or None if the
        batch isn't full yet. Pure CPU work — no asyncio/queue ops here.
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
        if len(audio) == 0:
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

        # Capture audio for manual recording (per-listener)
        if listener.is_recording:
            listener.record_buffer.append(merged.copy())

        # float32 [-1,1] → int16 PCM
        pcm = np.clip(merged, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        # Frame header: magic "AUD1" + sample_rate (u32 LE) + num_samples (u32 LE)
        header = struct.pack("<4sII", b"AUD1", int(listener._batch_sample_rate), len(pcm))
        return header + pcm.tobytes()

    def get_stats(self) -> dict:
        """Return broadcaster statistics."""
        return {
            "listeners": len(self._listeners),
            "chunks_sent": self._chunk_count,
            "bytes_sent": self._bytes_sent,
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