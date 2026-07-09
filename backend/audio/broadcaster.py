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
import json
import logging
import struct
import time
import uuid
from typing import Callable, Dict, List, Optional

import numpy as np

from backend.audio.demod import (
    DemodMode, StreamingDemodulator, compute_channel_power, iq_to_complex,
)
from backend.sdr.device_capture import AutoSquelchEstimator

logger = logging.getLogger(__name__)

# EMA smoothing factor for the per-listener signal level. A small value keeps
# the squelch decision stable so brief noise spikes don't "flutter" the gate.
# 0.2 ≈ average over ~5 chunks (~250ms at the default 50ms IQ cadence).
_SQUELCH_EMA = 0.2
# Hysteresis in dB: once the gate is open, the signal must drop this far BELOW
# the squelch threshold before it closes again. Prevents rapid open/close
# "chatter" when the signal hovers right at the threshold (classic squelch tail).
_SQUELCH_HYSTERESIS_DB = 4.0

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
# Bounded IQ input queue per listener. Tuned to absorb event-loop jitter and
# thread-pool scheduling latency without queueing SECONDS of stale audio.
# 4 was too tight: under load the worker would fall momentarily behind, the
# queue would fill, and ~37% of IQ packets got dropped — starving the demod so
# it produced audio slower than real-time (the "เสียงเร็ว" symptom, because the
# browser plays the sparse frames at full speed). 16 ≈ 0.8s of cushion at the
# default 50ms IQ cadence, large enough to ride out a GC pause / executor stall
# but small enough that a genuinely stuck listener still drops to near-real-time.
_IQ_QUEUE_MAX = 16
# How often (seconds) to push a per-listener "sig" text frame to the client's
# SIG meter while listening live. ~100ms (10 FPS) keeps the bar responsive
# without flooding the WebSocket. The level itself is updated every chunk; we
# only THROTTLE the wire send.
_SIG_FRAME_INTERVAL = 0.10
# Max number of IQ packets the per-listener worker merges into ONE demod call.
# Demod has a large per-call overhead (executor submit + filter warmup), so
# batching N chunks into one call is much cheaper than N separate calls. 8 is
# ~0.4s of audio at the 50ms cadence — large enough to amortize overhead, small
# enough to keep latency well under a second.
_WORKER_BATCH_MAX = 8

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
        # Per-listener SQUELCH (mute when signal below threshold).
        # Initialized from the global squelch value passed at subscribe time.
        # -120 dBFS is the "disabled" sentinel (pass everything, including
        # the noise floor / เสียงซ่า) so existing listeners are unaffected
        # unless a squelch value is explicitly set.
        self.squelch_db = -120.0
        # Auto-squelch (per-listener): when on, the threshold tracks the noise
        # floor adaptively via AutoSquelchEstimator (same estimator the JOB
        # path uses). Off by default; toggled from the Dashboard.
        self.auto_squelch = False
        self.auto_estimator: Optional[AutoSquelchEstimator] = None
        # Last measured channel power (dB) for THIS listener's VFO/channel.
        # Updated by _demod_listener_raw and consumed by _apply_squelch.
        self.last_signal_db = -120.0
        # EMA-smoothed signal level (dB), for a stable squelch decision.
        self._sig_smoothed = -120.0
        # Hysteresis gate: True while audio is currently passing (open).
        self._sq_open = False
        # stats
        self.frames_sent = 0
        self.created_at = time.time()
        # Drop diagnostics (incremented under event-loop thread only)
        self.iq_drops = 0       # IQ packets dropped on input (slow listener)
        self.queue_drops = 0    # AUD1 frames dropped on output (slow subscriber)
        # Per-listener SIG meter: throttle for the "sig" text frames pushed to
        # the client's SIG bar while listening live. The level itself lives in
        # last_signal_db / _sig_smoothed (updated every chunk in the worker).
        self._last_sig_send = 0.0
        # === Per-listener AUTO-RECORD (multi-user) ===
        # Each listener can independently auto-record the channel it is
        # listening to (its own VFO/mode/bandwidth), gated by its OWN squelch
        # threshold and measured signal — fully independent of every other
        # listener and of the global (center-channel) auto-record in SDRManager.
        # Master toggle for this listener's auto-record.
        self.auto_record = False
        # Squelch threshold (dB) used ONLY to gate this listener's auto-record.
        # Kept separate from `squelch_db` (which mutes the live LISTENING audio)
        # so a user can keep live squelch OFF (hear everything) while still only
        # recording when the signal is strong enough. <= -120 = always record.
        self.record_squelch_db = -120.0
        # Auto-record state machine (mirrors SDRManager._stream_loop TRACK 2):
        #   _ar_recording (bool)         currently buffering a recording
        #   _ar_buffer  (list[ndarray])  demodulated audio chunks
        #   _ar_start_time               monotonic start
        #   _ar_silence_start            monotonic when signal first went quiet
        self._ar_recording = False
        self._ar_buffer: list = []
        self._ar_start_time: Optional[float] = None
        self._ar_silence_start: Optional[float] = None

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

    def set_squelch(self, level_db: float) -> None:
        """Set this listener's squelch threshold (dB).

        Values <= -120 dBFS are treated as "squelch disabled" (pass all audio,
        including noise). Otherwise audio whose smoothed channel power is below
        this threshold is muted to silence.
        """
        self.squelch_db = max(-200.0, min(0.0, float(level_db)))
        # Force a re-evaluation on the next chunk by leaving _sq_open as-is;
        # the hysteresis logic in _apply_squelch will settle correctly.

    def set_auto_squelch(self, on: bool) -> None:
        """Toggle adaptive squelch for this listener.

        When ON, the squelch threshold tracks the rolling noise floor via
        AutoSquelchEstimator (the same estimator used by background recording
        jobs), so the user does not have to hand-tune the SQL slider as the
        band gets busy/quiet. When OFF, the manual squelch_db is used again.
        """
        self.auto_squelch = bool(on)
        if on:
            # (Re)create the estimator so it starts learning from scratch.
            self.auto_estimator = AutoSquelchEstimator()
        else:
            self.auto_estimator = None
        # Reset the gate so it re-evaluates against the new threshold source.
        self._sq_open = False

    def set_record_squelch(self, level_db: float) -> None:
        """Set the squelch threshold used to gate THIS listener's auto-record.

        Separate from set_squelch() (which gates live listening audio) so a user
        can listen with one squelch setting while recording on another. Values
        <= -120 disable the recording gate (record everything while enabled).
        """
        self.record_squelch_db = max(-200.0, min(0.0, float(level_db)))

    def enable_auto_record(self, on: bool) -> None:
        """Toggle per-listener auto-record.

        When turning OFF mid-recording, any buffered audio is finished (saved)
        on the next worker tick via the normal silence/timeout path; we just
        flip the flag so no NEW recording starts.
        """
        self.auto_record = bool(on)
        if not on:
            # Force-finish an in-flight recording immediately so it is not left
            # dangling when the user disables auto-record.
            self._ar_silence_start = -1.0  # sentinel: finish on next tick


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
        # Listener-count change callbacks. Fired (on the event-loop thread)
        # whenever a listener is added or removed so external components
        # (SDRManager) can react — e.g. promote/demote their pool consumer
        # priority. Callback signature: cb(listener_count: int).
        self._listener_count_callbacks: List[Callable[[int], None]] = []
        # Per-listener auto-record callbacks. Fired (on the event-loop thread)
        # when a listener's auto-record finishes a segment, with the same dict
        # shape stop_listener_recording() returns. The SDRManager registers one
        # to save the audio to disk + DB via save_listener_recording().
        self._auto_recording_callbacks: List[Callable] = []
        # Shared auto-record policy (kept in sync by SDRManager from its global
        # recording config). These are POLICY, not per-user — every listener's
        # auto-record obeys the same limits (min/max duration, silence timeout).
        self.ar_vad_silence_timeout = 3        # seconds of quiet → finish
        self.ar_max_duration = 300             # hard cap per segment
        self.ar_min_duration = 2               # shorter → discard

    def on_listener_count_change(self, callback: Callable[[int], None]) -> None:
        """Register a callback fired when the listener count changes.

        Used by SDRManager to promote its pool consumer to PRIORITY_LIVE when
        a listener connects, and demote it to PRIORITY_IDLE (yielding the
        tuner to background jobs) when the last listener leaves.

        Idempotent: registering the SAME callback object twice is a no-op.
        This guards against double-registration (e.g. SDRManager.__init__
        already registers, and a caller wiring it again) which would fire the
        callback multiple times per event and cause duplicate side effects.
        """
        if callback not in self._listener_count_callbacks:
            self._listener_count_callbacks.append(callback)

    def on_auto_recording(self, callback: Callable) -> None:
        """Register a callback fired when a listener's auto-record finishes.

        Signature: async def cb(listener: Listener, rec_data: dict) -> None
        where rec_data has the same keys as stop_listener_recording() returns
        (audio_data, duration, sample_rate, mode, vfo_offset_hz, user_id,
        user_role, source="auto"). SDRManager registers one to persist the
        recording. Best-effort: exceptions are logged, never raised.

        Idempotent: registering the SAME callback object twice is a no-op.
        Double-registration here previously caused every auto-record segment
        to be saved twice → duplicate rows in the recordings list.
        """
        if callback not in self._auto_recording_callbacks:
            self._auto_recording_callbacks.append(callback)

    def _fire_auto_recording(self, listener, rec_data: dict) -> None:
        """Fire all registered auto-recording callbacks (best-effort, async)."""
        for cb in self._auto_recording_callbacks:
            try:
                asyncio.create_task(cb(listener, rec_data))
            except Exception as e:
                logger.warning("🎙️ Auto-record callback error: %s", e)

    def _fire_listener_count_change(self, count: int) -> None:
        """Fire all registered listener-count-change callbacks (best-effort)."""
        for cb in self._listener_count_callbacks:
            try:
                cb(count)
            except Exception as e:
                logger.warning("🔊 Listener-count callback error: %s", e)

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
        # Notify SDRManager so it can promote its pool consumer to LIVE.
        self._fire_listener_count_change(len(self._listeners))
        return listener

    async def unsubscribe(self, listener: Listener) -> None:
        """Remove a listener and stop its background worker."""
        async with self._lock:
            existed = self._listeners.pop(id(listener), None)
        if existed is None:
            return
        # Finish any in-flight per-listener auto-record before tearing down so
        # a user disconnecting mid-transmission still saves what was captured.
        try:
            if listener._ar_recording:
                self._finish_listener_auto_record(
                    listener, listener.audio_rate, reason="disconnect"
                )
        except Exception as e:
            logger.warning("🎙️ Listener %s auto-record finish-on-unsub failed: %s",
                           listener.id[:8], e)
        task = listener.task
        listener.task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("🔊 Audio listener removed (total: %d)", len(self._listeners))
        # Notify SDRManager so it can demote its pool consumer to IDLE (which
        # yields the tuner to background jobs when the last listener leaves).
        self._fire_listener_count_change(len(self._listeners))

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
                g_vfo_offset_hz: Optional[float] = None,
                g_signal_level: Optional[float] = None) -> None:
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

        `g_signal_level` (optional) is the channel power (dB) already
        computed by SDRManager for the global channel, so listeners reusing
        the global audio can update their squelch state without recomputing.
        """
        if not self._listeners:
            return
        self._chunk_count += 1
        pkt = (iq_bytes, int(sample_rate), int(audio_rate),
               global_audio, g_mode, g_channel_bw, g_vfo_offset_hz,
               g_signal_level)
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

        BATCHED DEMOD (important): when a listener listens to a frequency that
        does NOT match the global demod (its own VFO offset), each chunk costs
        ~23 ms of CPU in the executor + ~2 ms of submit/context-switch overhead
        per call. At the ~50 ms IQ cadence that leaves no headroom and the iq
        queue overflows → IQ drops → demod produces audio SLOWER than real-time
        → the browser plays the sparse frames at full speed, heard as
        sped-up / choppy audio. To fix this we DRAIN every queued packet in one
        go and demod them as ONE merged buffer in a single executor call, which
        amortizes the per-call overhead across many chunks (e.g. 4 chunks → 1
        call of ~80 ms instead of 4 calls of ~25 ms each). The merged audio is
        then re-split per chunk so squelch/batch/SIG still run per-chunk.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                pkt = await ln.iq_queue.get()
                # Drain any additional packets already queued so we can process
                # them as a batch (reduces executor overhead and keeps the
                # listener near real time). Cap to avoid unbounded latency.
                batch = [pkt]
                while len(batch) < _WORKER_BATCH_MAX:
                    try:
                        batch.append(ln.iq_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Separate "reuse global" packets from "own demod" packets.
                # Reuse packets are cheap (no executor); own-demod packets are
                # merged into a single executor call.
                reuse_pkts = []
                demod_pkts = []
                last_sample_rate = None
                last_audio_rate = None
                for p in batch:
                    (iq_bytes, sample_rate, audio_rate,
                     global_audio, g_mode, g_channel_bw, g_vfo,
                     g_signal_level) = p
                    last_sample_rate = sample_rate
                    last_audio_rate = audio_rate
                    if self._matches_global(
                        ln, sample_rate, audio_rate,
                        global_audio, g_mode, g_channel_bw, g_vfo,
                    ):
                        reuse_pkts.append((global_audio, g_signal_level))
                    else:
                        demod_pkts.append(iq_bytes)

                # Demod ALL own-demod packets in ONE executor call (merged).
                demod_audios = []
                if demod_pkts:
                    try:
                        merged_iq = b"".join(demod_pkts)
                        merged_audio = await loop.run_in_executor(
                            self._executor, self._demod_listener_raw,
                            ln, merged_iq, last_sample_rate, last_audio_rate,
                        )
                        # Split the merged audio back per-chunk by ratio of the
                        # input byte lengths (each chunk has the same IQ size
                        # in practice, so this is an even split).
                        demod_audios = self._split_audio_by_iq(
                            merged_audio, demod_pkts)
                    except Exception as e:
                        logger.warning(
                            "🔊 Listener %s demod error: %s", ln.id[:8], e
                        )
                        # On demod failure, skip these chunks but keep flowing.
                        demod_audios = [None] * len(demod_pkts)

                # Now process each original packet in order, attaching its
                # audio (reused global, or the per-chunk slice of merged demod).
                di = 0  # index into demod_audios
                ri = 0  # index into reuse_pkts
                for p in batch:
                    (iq_bytes, sample_rate, audio_rate,
                     global_audio, g_mode, g_channel_bw, g_vfo,
                     g_signal_level) = p
                    if self._matches_global(
                        ln, sample_rate, audio_rate,
                        global_audio, g_mode, g_channel_bw, g_vfo,
                    ):
                        audio, gsig = reuse_pkts[ri]
                        ri += 1
                        # Update the squelch signal level from the global value.
                        if gsig is not None:
                            ln.last_signal_db = float(gsig)
                            ln._sig_smoothed = (
                                _SQUELCH_EMA * float(gsig)
                                + (1.0 - _SQUELCH_EMA) * ln._sig_smoothed
                            )
                    else:
                        audio = demod_audios[di]
                        di += 1

                    if audio is None or len(audio) == 0:
                        continue

                    # === Per-listener AUTO-RECORD (multi-user) ===
                    # NOTE: uses PRE-squelch audio (the real content) so a user
                    # with live squelch OFF still records complete audio.
                    # Also process when _ar_recording is True even if auto_record
                    # was just disabled — so the sentinel (-1.0) triggers finish.
                    if ln.auto_record or ln._ar_recording:
                        self._process_auto_record(ln, audio, audio_rate)

                    # Apply per-listener squelch + batch into an AUD2 frame.
                    # Done BEFORE the SIG frame so the squelch gate state
                    # (_sq_open) reflects THIS chunk — the frontend uses it to
                    # flash the VFO row green when audio is passing.
                    audio = self._apply_squelch(ln, audio)

                    # === Per-listener SIG meter (live listening) ===
                    # Throttled to ~10 FPS. Carries the smoothed signal level
                    # AND the current squelch-gate state so the client can show
                    # a "RX active" indicator (flashing green) on the VFO row.
                    now = time.monotonic()
                    if now - ln._last_sig_send >= _SIG_FRAME_INTERVAL:
                        ln._last_sig_send = now
                        self._enqueue_frame(ln, ("sig", {
                            "db": round(ln._sig_smoothed, 1),
                            "sq_open": bool(ln._sq_open),
                        }))

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
    def _split_audio_by_iq(merged_audio: np.ndarray,
                           iq_chunks: list) -> list:
        """Split merged demod audio back into per-chunk slices.

        The merged audio came from concatenating N IQ chunks and demodulating
        once. Because demodulation is (approximately) linear in length, we split
        the output proportionally to each input chunk's byte length. In practice
        all chunks in a batch are the same length, so this is an even split.
        """
        if merged_audio is None or len(merged_audio) == 0:
            return [np.zeros(0, dtype=np.float32)] * len(iq_chunks)
        total_bytes = sum(len(c) for c in iq_chunks)
        if total_bytes <= 0:
            return [np.zeros(0, dtype=np.float32)] * len(iq_chunks)
        out = []
        offset = 0
        n_audio = len(merged_audio)
        for c in iq_chunks:
            frac = len(c) / total_bytes
            take = int(round(n_audio * frac))
            end = min(offset + take, n_audio)
            out.append(merged_audio[offset:end])
            offset = end
        # If rounding left a tail, attach it to the last chunk.
        if offset < n_audio and out:
            out[-1] = np.concatenate([out[-1], merged_audio[offset:]])
        return out

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

        Also computes the per-listener channel power (dB) so that squelch can
        gate the audio on the loop thread. We reuse the same IQ the demodulator
        consumes; compute_channel_power() applies its own VFO shift internally,
        matching what the demodulator decodes.
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

        # Parse the raw IQ bytes ONCE into a complex array and reuse it for
        # BOTH the channel-power measurement (squelch/SIG) and demodulation.
        # Previously each step called iq_to_complex() separately — two O(n)
        # bytes→complex passes per chunk, plus an extra FFT — which doubled the
        # per-listener CPU and was the main cause of iq_queue overflow (audio
        # produced slower than real-time → the "เสียงเร็ว" symptom).
        try:
            iq = iq_to_complex(iq_bytes)
            level = compute_channel_power(
                iq, sample_rate, listener.mode,
                channel_bw=listener.channel_bw,
                vfo_offset_hz=listener.vfo_offset_hz,
            )
            listener.last_signal_db = level
            # EMA smoothing for a stable squelch decision.
            listener._sig_smoothed = (
                _SQUELCH_EMA * level + (1.0 - _SQUELCH_EMA) * listener._sig_smoothed
            )
        except Exception:
            # Non-fatal: leave the previous level. Still need a complex array
            # for demod, so parse defensively here.
            try:
                iq = iq_to_complex(iq_bytes)
            except Exception:
                return np.zeros(0, dtype=np.float32)

        # Pass the already-parsed complex array so demod skips its own
        # iq_to_complex() call (the process() overload accepts complex).
        audio = listener.demod.process(
            iq, listener.mode, sample_rate, audio_rate,
            channel_bw=listener.channel_bw,
        )
        return audio

    @staticmethod
    def _apply_squelch(listener: Listener, audio: np.ndarray) -> np.ndarray:
        """Gate audio through this listener's squelch (in-place decision).

        Returns the original audio if the gate is open, or a zero array of the
        same shape (silence) if closed. Sending silence (rather than dropping
        the frame) keeps the client's playback clock continuous so there are
        no underrun clicks at the open/close transitions.

        Two modes:
          - AUTO: the threshold tracks the rolling noise floor via
            AutoSquelchEstimator (open = floor+margin_open, close =
            floor+margin_close). The estimator is fed the raw per-chunk level
            here so it keeps learning regardless of which demod path produced
            the level. Hysteresis comes from the estimator's two thresholds.
          - MANUAL: a fixed user threshold with a fixed hysteresis band
            (_SQUELCH_HYSTERESIS_DB) below it. The -120 dB sentinel disables
            squelch entirely (pass all audio).
        """
        # === AUTO mode ===
        if listener.auto_squelch and listener.auto_estimator is not None:
            # Feed the latest measured level so the estimator keeps tracking
            # the noise floor (uses raw last_signal_db, matching the JOB path;
            # the estimator's own rolling window does the smoothing).
            listener.auto_estimator.update(
                listener.last_signal_db, frozen=listener._sq_open
            )
            open_thr = listener.auto_estimator.open_threshold_db
            close_thr = listener.auto_estimator.close_threshold_db
            level = listener._sig_smoothed
            if not listener._sq_open:
                if level >= open_thr:
                    listener._sq_open = True
            else:
                if level < close_thr:
                    listener._sq_open = False
            return audio if listener._sq_open else np.zeros_like(audio)

        # === MANUAL mode ===
        # Squelch disabled (sentinel) → always pass audio.
        if listener.squelch_db <= -120.0:
            return audio

        level = listener._sig_smoothed
        if not listener._sq_open:
            # Gate closed: open when signal rises above the threshold.
            if level >= listener.squelch_db:
                listener._sq_open = True
        else:
            # Gate open: close only when signal drops below threshold - hyst.
            if level < listener.squelch_db - _SQUELCH_HYSTERESIS_DB:
                listener._sq_open = False

        if listener._sq_open:
            return audio
        # Mute to silence (preserve length so the client clock stays aligned).
        return np.zeros_like(audio)

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

    def _enqueue_frame(self, ln: Listener, frame) -> None:
        """Push a finished frame to the listener's output queue.

        `frame` is either:
          - bytes  → an AUD2 audio frame (sent as a WebSocket binary frame)
          - tuple  → ("sig", <db:float>) control text frame for the SIG meter
                     (sent as a WebSocket text frame)

        Drop-oldest on overflow so a slow WebSocket subscriber cannot
        accumulate seconds of stale audio (which would otherwise manifest as
        ever-growing latency — the "ฟลุด/ต้องกดฟังสดใหม่" symptom). When
        dropping, prefer to drop a "sig" text frame over an audio frame so a
        slow client never loses audio continuity just because SIG frames piled
        up.
        """
        if isinstance(frame, bytes):
            ln.frames_sent += 1
            self._bytes_sent += len(frame)
        self._last_audio_time = time.time()
        try:
            ln.queue.put_nowait(frame)
        except asyncio.QueueFull:
            ln.queue_drops += 1
            # Drop-oldest on overflow so a slow subscriber cannot accumulate
            # stale audio. (A "sig" text frame is ~20 bytes; dropping either
            # kind here is fine — real-time data is replaceable.)
            try:
                ln.queue.get_nowait()
            except Exception:
                pass
            try:
                ln.queue.put_nowait(frame)
            except Exception:
                ln.queue_drops += 1

    def _process_auto_record(self, ln: Listener, audio: np.ndarray,
                             audio_rate: int) -> None:
        """Per-listener auto-record state machine (runs on the event-loop thread).

        Mirrors SDRManager._stream_loop TRACK 2 but uses THIS listener's own
        measured signal (`_sig_smoothed`) and its OWN `record_squelch_db` gate,
        so each user auto-records the channel they are actually listening to,
        fully independent of every other user and of the global auto-record.

        `audio` is the PRE-squelch demodulated signal (the real content), so a
        user who keeps live listening squelch OFF still records complete audio.
        """
        level = ln._sig_smoothed
        # Squelch gate: <= -120 dB means "always record while enabled".
        gate_open = ln.record_squelch_db <= -120.0 or level >= ln.record_squelch_db
        now = time.monotonic()

        if gate_open:
            if not ln._ar_recording:
                # Signal rose above the gate → start a new segment.
                ln._ar_recording = True
                ln._ar_buffer = []
                ln._ar_start_time = now
                ln._ar_silence_start = None
                logger.info(
                    "🎙️ Listener %s auto-record started (signal %.1f dB, user=%s)",
                    ln.id[:8], level, ln.user_id,
                )
            else:
                # Already recording and still above gate → reset silence timer.
                ln._ar_silence_start = None
            ln._ar_buffer.append(audio.copy())
        else:
            if ln._ar_recording:
                # Below gate: accumulate the tail and count silence.
                ln._ar_buffer.append(audio.copy())
                if ln._ar_silence_start is None:
                    ln._ar_silence_start = now
                silence = now - ln._ar_silence_start
                # Finish on silence timeout, OR if the user just disabled
                # auto-record (_ar_silence_start sentinel == -1.0).
                disabled = ln._ar_silence_start < 0
                if disabled or silence >= self.ar_vad_silence_timeout:
                    self._finish_listener_auto_record(ln, audio_rate,
                                                      reason="silence" if not disabled else "disabled")

        # Hard duration cap.
        if ln._ar_recording and ln._ar_start_time is not None:
            elapsed = now - ln._ar_start_time
            if elapsed >= self.ar_max_duration:
                self._finish_listener_auto_record(ln, audio_rate, reason="max_duration")

    def _finish_listener_auto_record(self, ln: Listener, audio_rate: int,
                                     reason: str = "silence") -> None:
        """Finish a listener's in-flight auto-record segment and fire callbacks.

        Discards segments shorter than ar_min_duration (noise blips). Resets the
        listener's auto-record state machine so the next above-gate signal
        starts a fresh segment.
        """
        if not ln._ar_recording:
            return
        ln._ar_recording = False
        buf = ln._ar_buffer
        start = ln._ar_start_time
        ln._ar_buffer = []
        ln._ar_start_time = None
        ln._ar_silence_start = None
        if not buf:
            return

        audio_data = np.concatenate(buf)
        duration = len(audio_data) / max(1, audio_rate)
        if duration < self.ar_min_duration:
            logger.info(
                "🎙️ Listener %s auto-record discarded (too short: %.1fs)",
                ln.id[:8], duration,
            )
            return

        rec_data = {
            "audio_data": audio_data,
            "duration": duration,
            "sample_rate": audio_rate,
            "mode": ln.mode.value,
            "vfo_offset_hz": ln.vfo_offset_hz,
            "user_id": ln.user_id,
            "user_role": ln.user_role,
            "channel_bw": int(ln.channel_bw),
            "source": "auto",
        }
        logger.info(
            "🎙️ Listener %s auto-record finished (%.1fs, reason=%s, user=%s)",
            ln.id[:8], duration, reason, ln.user_id,
        )
        self._fire_auto_recording(ln, rec_data)

    def start_auto_record(self, listener: Listener, squelch_db: Optional[float] = None) -> bool:
        """Enable per-listener auto-record (optionally setting the record squelch).

        Returns True (always enabled after this call).
        """
        if squelch_db is not None:
            listener.set_record_squelch(float(squelch_db))
        listener.enable_auto_record(True)
        logger.info(
            "🎙️ Listener %s auto-record ENABLED (squelch=%.1f dB, user=%s)",
            listener.id[:8], listener.record_squelch_db, listener.user_id,
        )
        return True

    def stop_auto_record(self, listener: Listener) -> None:
        """Disable per-listener auto-record (finishes any in-flight segment)."""
        listener.enable_auto_record(False)
        # Finish an in-flight recording immediately on the next worker tick;
        # enable_auto_record(False) already sets the sentinel for that.
        logger.info(
            "🎙️ Listener %s auto-record DISABLED (user=%s)",
            listener.id[:8], listener.user_id,
        )

    def get_listener_recordings(self) -> list:
        """Return per-listener recording status (manual + auto) for dashboard.

        Enumerates every connected audio listener and reports whether it is
        currently doing a manual recording or a per-listener auto-record, plus
        the channel (mode / VFO offset / bandwidth) and owning user. Consumed
        by SDRManager.status → frontend dashboard indicator below the clock.
        """
        result = []
        for ln in self._listeners.values():
            entry = {
                "listener_id": ln.id[:8],
                "user_id": ln.user_id,
                "user_role": ln.user_role,
                "mode": ln.mode.value,
                "vfo_offset_hz": round(ln.vfo_offset_hz, 1),
                "channel_bw": int(ln.channel_bw),
                "manual_recording": bool(ln.is_recording),
                "auto_record_enabled": bool(ln.auto_record),
                "auto_recording": bool(ln._ar_recording),
            }
            result.append(entry)
        return result

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
            "channel_bw": int(listener.channel_bw),
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