"""
DeviceCapture — background IQ capture + per-frequency recording for one SDR device.

IMPORTANT (shared tuner): Each DeviceCapture acquires a SHARED consumer from
the DeviceConnectionPool. There is exactly ONE RtlTcpClient (and therefore
ONE tuner: center freq / sample rate / gain / ppm) per physical device,
shared between interactive listening (SDRManager, PRIORITY_LIVE) and this
background recording (PRIORITY_JOB).

Because the tuner is shared:
  * When a LIVE listener claims the tuner (retunes/changes ppm), this JOB
    is preempted via on_preempt() and pauses. Any in-progress recording is
    finished so the saved audio is not spliced across frequencies.
  * When the LIVE listener releases the tuner, this JOB reclaims it and
    RE-APPLIES its own center freq / sample rate / gain / ppm, so the
    recorded frequency is correct and uses the JOB's ppm — not whatever the
    user happened to set while listening live.

It tunes the shared device to the window's center frequency
((start_freq + sample_rate) / 2), then continuously reads fanned-out IQ.

For each target frequency (which must lie within [start_freq, start_freq +
sample_rate]) it:
  - demodulates at offset (freq - center) using the configured mode/bandwidth
  - computes signal level within that bandwidth
  - applies squelch: starts buffering when signal > squelch, finishes and
    saves a WAV when signal drops below squelch for `silence_timeout` seconds
    (or hits `max_recording_duration`).

This module is used by JobScheduler (backend/sdr/job_scheduler.py) to run
recording jobs in the background. See connection_pool.py for arbitration.
"""
import asyncio
import concurrent.futures
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

import numpy as np
import soundfile as sf

from backend.config import settings
from backend.sdr.rtl_tcp import RtlTcpClient
from backend.sdr.connection_pool import (
    pool as connection_pool, PRIORITY_JOB, PRIORITY_LIVE,
)
# notify_live_tuner_changed is imported lazily inside _reclaim_tuner to avoid
# a circular import (manager.py imports from this package's connection_pool).
from backend.audio.demod import DemodMode, StreamingDemodulator, compute_signal_level
from backend.database import async_session
from backend.models.recording import Recording, RecordingStatus
from backend.models.recording_job import RecordingJob, RecordingJobWindow, RecordingJobFrequency

logger = logging.getLogger(__name__)

# State machine states for DeviceCapture
STATE_IDLE = "IDLE"
STATE_RUNNING = "RUNNING"
STATE_PAUSED = "PAUSED"
STATE_STOPPING = "STOPPING"


def _percentile(values: list, pct: float) -> float:
    """Linear-interpolated percentile of a list of floats."""
    if not values:
        return -115.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


class AutoSquelchEstimator:
    """Adaptive squelch threshold from a rolling per-channel noise floor.

    Why a low percentile (not a mean):
      Voice comms are silent ~50% of the time, so the *mean* level gets
      pulled up by the signal itself. A low percentile (p15-p25) tracks the
      quiet/noise baseline even while the channel is intermittently active,
      which is exactly what a squelch threshold must sit just above.

    Continuous learning (no full freeze):
      Previously the estimator FROZE entirely while recording. Under dense
      adjacent-channel traffic, leakage inflated the floor and a single
      false-open would freeze the (already-too-high) threshold for the
      whole recording → long noise files ("freeze-lock trap"). Now we keep
      learning but switch to a LOWER percentile (p15) while active so a real
      transmission cannot drag the floor up much, while the floor can still
      DROP when noise subsides (unfreezing the gate on a stuck-open recording).

    Hysteresis:
      Two thresholds are exposed: open (higher) and close (lower). The gate
      must cross open to START recording and drop below close to END it. This
      prevents rapid open/close chatter when the level oscillates around a
      single threshold — which previously reset the silence timer endlessly
      and produced over-long spliced recordings.

    Inputs are channel signal levels in dB produced by compute_signal_level()
    (one call per ~50 ms IQ chunk). The thresholds are:
        open  = clamp(noise_floor + margin_open_db,  min_db, max_db)
        close = clamp(noise_floor + margin_close_db, min_db, max_db)
    """

    def __init__(
        self,
        window_seconds: float = 8.0,
        chunk_seconds: float = 0.05,
        percentile: float = 25.0,
        active_percentile: float = 15.0,
        margin_open_db: float = 8.0,
        margin_close_db: float = 2.0,
        min_db: float = -115.0,
        max_db: float = -20.0,
    ):
        self._n = max(8, int(window_seconds / max(chunk_seconds, 1e-3)))
        self._buf: List[float] = [0.0] * self._n
        self._idx = 0
        self._filled = False
        self._pct = float(percentile)
        self._active_pct = float(active_percentile)
        self._margin_open = float(margin_open_db)
        self._margin_close = float(margin_close_db)
        self._min = float(min_db)
        self._max = float(max_db)
        # Start CLOSED (max_db) during warmup so no recording opens before we
        # have enough samples to estimate a real noise floor.
        self._floor = self._min
        self._open_thr = self._max
        self._close_thr = self._max
        self._last_compute = 0.0
        self._min_samples_to_compute = 4

    def update(self, level_db: float, frozen: bool = False,
               now: Optional[float] = None) -> None:
        """Feed one level sample and (re)compute thresholds if due.

        `frozen` is kept for API compatibility but is now interpreted as
        "active" — we still learn, just with a lower percentile so a real
        transmission does not pull the floor up much.
        """
        now = now if now is not None else time.time()
        # Always feed the sample so the rolling window stays current. When
        # "frozen" (recording active) use the lower percentile so genuine
        # signal does not inflate the floor.
        self._buf[self._idx] = level_db
        self._idx = (self._idx + 1) % self._n
        if self._idx == 0:
            self._filled = True
        # Recompute at most ~2 Hz.
        if now - self._last_compute >= 0.5:
            samples = self._samples()
            if len(samples) >= self._min_samples_to_compute:
                self._last_compute = now
                pct = self._active_pct if frozen else self._pct
                self._floor = _percentile(samples, pct)
                self._open_thr = max(self._min, min(self._max, self._floor + self._margin_open))
                self._close_thr = max(self._min, min(self._max, self._floor + self._margin_close))

    def _samples(self) -> List[float]:
        return self._buf if self._filled else self._buf[:self._idx]

    @property
    def noise_floor_db(self) -> float:
        return self._floor

    @property
    def open_threshold_db(self) -> float:
        """Gate must cross this (rising) to START a recording."""
        return self._open_thr

    @property
    def close_threshold_db(self) -> float:
        """Gate must drop below this (falling) to END a recording."""
        return self._close_thr

    @property
    def effective_squelch_db(self) -> float:
        # Back-compat alias: midpoint of the hysteresis band. Callers that
        # only need a single representative value (e.g. display) use this.
        return (self._open_thr + self._close_thr) * 0.5

    def reset(self) -> None:
        self._buf = [0.0] * self._n
        self._idx = 0
        self._filled = False
        self._floor = self._min
        # Match __init__: start CLOSED during warmup.
        self._open_thr = self._max
        self._close_thr = self._max
        self._last_compute = 0.0


class _FrequencyChannel:
    """Per-target-frequency state inside a DeviceCapture."""

    def __init__(self, freq: RecordingJobFrequency, center_hz: int, audio_rate: int):
        self.frequency_hz = freq.frequency_hz
        self.offset_hz = float(freq.frequency_hz - center_hz)
        # Manual (configured) squelch; used directly when auto_squelch is off,
        # and reported for display even when auto is on.
        self.squelch_db = float(freq.squelch_db)
        self.auto_squelch = bool(getattr(freq, "auto_squelch", False))
        # Adaptive estimator; None when manual squelch is used.
        self.auto_estimator: Optional[AutoSquelchEstimator] = (
            AutoSquelchEstimator() if self.auto_squelch else None
        )
        self.demod_mode = DemodMode(freq.demod_mode.upper())
        self.bandwidth_hz = int(freq.bandwidth_hz)
        self.label = freq.label
        self.audio_rate = audio_rate
        # VFO passband margin ratio (default 0.45; overridden by DeviceCapture
        # from the System Policy "sdr_vfo_margin_ratio" app setting).
        self._margin_ratio: float = 0.45
        # Streaming demodulator carrying filter state across chunks
        self.demod = StreamingDemodulator()
        self.demod.set_vfo_offset(self.offset_hz)
        # Recording state
        self.buffer: List[np.ndarray] = []
        self.is_recording = False
        self.start_time: Optional[float] = None
        self.silence_start: Optional[float] = None
        self.signal_level = -120.0
        # Track whether this channel is currently reachable given the
        # actual (possibly LIVE-controlled) center frequency.
        self.in_passband: bool = True

    @property
    def effective_squelch_db(self) -> float:
        """The threshold actually used for open/close decisions this tick."""
        if self.auto_estimator is not None:
            return self.auto_estimator.effective_squelch_db
        return self.squelch_db

    @property
    def noise_floor_db(self) -> Optional[float]:
        if self.auto_estimator is not None:
            return self.auto_estimator.noise_floor_db
        return None

    def update_offset(self, actual_center_hz: int, sample_rate: int) -> bool:
        """Recompute offset from the ACTUAL center frequency.

        Returns True if the channel is within the usable passband.
        When the LIVE consumer retunes the shared device, our intended
        center may differ — but our target frequency may still be
        reachable at a different offset. If it moved significantly during
        a recording, the caller should finish that recording to avoid
        splicing audio from different frequencies.
        """
        # Use the same margin ratio as the System Policy (sdr_vfo_margin_ratio),
        # defaulting to 0.45 only if the setting hasn't been loaded yet.
        # This was previously hardcoded to 0.45, which was NARROWER than the
        # System Policy value (e.g. 0.49) → channels near the passband edge
        # were incorrectly marked "out of passband" and skipped → stuck at
        # -120 dB in the dashboard.
        ratio = getattr(self, '_margin_ratio', 0.45)
        max_offset = sample_rate * ratio
        new_offset = float(self.frequency_hz - actual_center_hz)
        if abs(new_offset) > max_offset:
            self.in_passband = False
            return False
        self.in_passband = True
        # Update demodulator offset if it changed by more than 1 Hz
        if abs(new_offset - self.offset_hz) > 1.0:
            self.offset_hz = new_offset
            self.demod.set_vfo_offset(new_offset)
        return True

    def reset_mode(self):
        """Reset demodulator (call when mode/bw change)."""
        # StreamingDemodulator self-detects changes, but we keep this hook.
        pass


class DeviceCapture:
    """Background capture for one SDR device, driven by a RecordingJob.

    Lifecycle:
      - start(): connect + spawn stream task
      - stop(): cancel task + disconnect
    """

    def __init__(
        self,
        job: RecordingJob,
        window: RecordingJobWindow,
        silence_timeout: int = 5,
        max_recording_duration: int = 300,
        min_recording_duration: float = 2.0,
    ):
        self.job_id = job.id
        self.device_id = job.device_id
        self.window_id = window.id
        self.gain = window.gain
        self.ppm = window.freq_correction_ppm
        self.start_freq_hz = window.start_freq_hz
        self.sample_rate = window.sample_rate
        self.center_hz = self.start_freq_hz + self.sample_rate // 2

        self.silence_timeout = silence_timeout
        self.max_recording_duration = max_recording_duration
        self.min_recording_duration = min_recording_duration
        self.audio_rate = settings.AUDIO_SAMPLE_RATE

        # Build frequency channels FIRST so len(self.channels) is valid when
        # sizing the DSP thread pool below. (Previously self.channels was
        # referenced in the ThreadPoolExecutor's max_workers= BEFORE it was
        # assigned, raising AttributeError on every job start and silently
        # breaking ALL scheduled recordings.)
        self.channels: List[_FrequencyChannel] = [
            _FrequencyChannel(f, self.center_hz, self.audio_rate)
            for f in (window.frequencies or [])
        ]

        # Dedicated thread pool for the per-channel CPU-heavy DSP step
        # (compute_signal_level ~21 ms + demod.process ~15 ms at 2.4 MSPS, per
        # channel per 50 ms chunk). With N channels that's N × ~36 ms every
        # 50 ms — e.g. 3 channels = ~108 ms, over 2× the per-chunk budget.
        # Running that INLINE on the asyncio event loop pegged the process at
        # 100% CPU and starved every other coroutine (logins, the Recordings
        # list, live audio underruns). Offloading to worker threads frees the
        # loop; numpy/scipy release the GIL during the FFT/linalg work. One
        # worker per channel lets all channels process in parallel.
        self._dsp_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(2, len(self.channels)),
            thread_name_prefix=f"job-dsp-{job.id[:8]}",
        )

        # Resolve device connection params from the SdrDevice DB row.
        # Defaults are set here so acquire() in start() never hits a missing
        # attribute (which previously raised AttributeError and silently broke
        # ALL background recording jobs).
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._device_type: str = "rtl_tcp"
        self._device_index: Optional[int] = None
        self._serial: Optional[str] = None
        self.client = None  # SharedConnectionConsumer (from pool)
        self._consumer_id: str = f"job:{self.job_id}"
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._state: str = STATE_IDLE

    async def _resolve_device_addr(self) -> None:
        """Load connection params (host/port or device_index/serial) + type
        from the SdrDevice DB row.
        """
        from backend.models.sdr_device import SdrDevice
        async with async_session() as session:
            dev = await session.get(SdrDevice, self.device_id)
            if dev is None:
                raise RuntimeError(f"Device {self.device_id} not found")
            cfg = dev.get_config()
            # Device type determines which connection params are relevant.
            self._device_type = (getattr(dev, 'type', None) or 'rtl_tcp').lower()
            if self._device_type == "usb":
                # USB RTL-SDR: identify by serial (preferred) or device_index.
                serial = str(cfg.get("serial") or "").strip()
                self._serial = serial or None
                try:
                    self._device_index = int(cfg.get("device_index", 0))
                except (TypeError, ValueError):
                    self._device_index = 0
                # host/port are irrelevant for USB but keep sane defaults so
                # the pool constructor (which accepts them for rtl_tcp) does
                # not receive None.
                self._host = "usb"
                self._port = 0
            else:
                # rtl_tcp (default): identify by host:port.
                self._host = str(cfg.get("host") or settings.RTL_TCP_HOST).strip()
                self._port = int(cfg.get("port") or settings.RTL_TCP_PORT)

    @property
    def _shared_client(self) -> Optional[RtlTcpClient]:
        """The single underlying RtlTcpClient shared by all consumers.

        The connection pool fans out IQ to each consumer, but the tuner
        (freq/sample_rate/gain/ppm) lives on THIS shared client. We must read
        the ACTUAL tuner state from here (not from our consumer's mirror,
        which goes stale the moment a LIVE consumer retunes the device).
        """
        try:
            return self.client._conn.client if self.client else None
        except Exception:
            return None

    def _is_controller(self) -> bool:
        """True if this JOB consumer currently owns the shared tuner."""
        try:
            return self.client._conn._controller_id == self._consumer_id
        except Exception:
            return False

    async def start(self) -> None:
        """Connect via the shared pool and start the background capture loop."""
        if self._running:
            return
        await self._resolve_device_addr()
        # Acquire a shared consumer with JOB priority (yields to LIVE)
        self.client = await connection_pool.acquire(
            self.device_id, self._host, self._port,
            consumer_id=self._consumer_id, priority=PRIORITY_JOB,
            device_type=self._device_type,
            device_index=self._device_index,
            serial=self._serial,
        )
        # Register the preemption callback so we get paused when a LIVE
        # consumer claims the tuner, and can reclaim it when LIVE leaves.
        # Without this, on_preempt() was never wired up (dead code) and JOB
        # kept demodulating at stale offsets while the user listened elsewhere.
        try:
            self.client._conn.on_preempt(self._on_preempt)
        except Exception as e:
            logger.warning(
                f"DeviceCapture: failed to register preempt callback: {e}"
            )
        # Preconfigure tuner values before connecting
        self.client.preconfigure(self.center_hz, self.sample_rate, self.gain, self.ppm)
        ok = await self.client.connect()
        if not ok:
            # Clean up: release the consumer so the pool can tear down the
            # failed connection (otherwise refcount leak prevents reconnect).
            try:
                await connection_pool.release(self.device_id, self._consumer_id)
            except Exception:
                pass
            self.client = None
            raise RuntimeError(
                f"DeviceCapture: failed to connect {self._host}:{self._port}"
            )
        # Load the VFO passband margin ratio from the System Policy setting
        # (sdr_vfo_margin_ratio) so the JOB uses the SAME margin as the LIVE
        # side. Previously this was hardcoded to 0.45, which was narrower
        # than the configured 0.49 → channels near the passband edge were
        # incorrectly marked "out of passband" and skipped → stuck at -120 dB.
        try:
            from backend.services.app_settings import get_setting
            raw_ratio = await get_setting("sdr_vfo_margin_ratio", "0.49")
            ratio = float(raw_ratio)
            if 0.40 <= ratio <= 0.49:
                for ch in self.channels:
                    ch._margin_ratio = ratio
                logger.info(f"📐 [job={self.job_id}] VFO margin ratio: {ratio}")
        except Exception as e:
            logger.warning(f"Failed to load VFO margin ratio: {e}")

        # CRITICAL FIX: explicitly apply tuner settings to the HARDWARE.
        # preconfigure() only writes Python mirrors on the shared RtlTcpClient;
        # it does NOT send commands to the dongle. And _ensure_connected()
        # short-circuits (returns True) when the connection is ALREADY up
        # (e.g. the LIVE autostart consumer connected first), so the tuner
        # stays on whatever the LIVE consumer last set. We must claim
        # controller + apply our own freq/sample_rate/gain/ppm explicitly so
        # the recorded frequency is correct. This was the root cause of
        # "เสียงซ่าๆ": the JOB demodulated at the LIVE center frequency.
        await self._reclaim_tuner()
        # Log the ACTUAL tuner state on the shared device so we can verify
        # the hardware is tuned to our target.
        shared = self._shared_client
        if shared is not None:
            logger.info(
                f"📼 DeviceCapture started: job={self.job_id} device={self.device_id} "
                f"WANTED center={self.center_hz/1e6:.4f} MHz sr={self.sample_rate} "
                f"gain={self.gain} ppm={self.ppm} | "
                f"ACTUAL tuner freq={shared._frequency/1e6:.4f} MHz "
                f"sr={shared._sample_rate} gain={shared._gain} ppm={shared._ppm} | "
                f"{len(self.channels)} freq(s)"
            )
            for ch in self.channels:
                off = ch.frequency_hz - shared._frequency
                pct = abs(off) / (shared._sample_rate / 2.0) * 100.0 if shared._sample_rate else 0
                logger.info(
                    f"   📻 {ch.frequency_hz/1e6:.4f} MHz ({ch.label}) "
                    f"offset={off/1e3:+.2f} kHz ({pct:.0f}% of half-band) "
                    f"{ch.demod_mode.value} bw={ch.bandwidth_hz} squelch={ch.squelch_db}dB"
                )
        else:
            logger.info(
                f"📼 DeviceCapture started: job={self.job_id} device={self.device_id} "
                f"center={self.center_hz/1e6:.4f} MHz sr={self.sample_rate} "
                f"{len(self.channels)} freq(s) (no shared client for logging)"
            )
        self._running = True
        self._state = STATE_RUNNING
        self._task = asyncio.create_task(self._stream_loop())

    async def stop(self) -> None:
        """Stop capture loop, finish recordings, release pool consumer."""
        self._running = False
        self._state = STATE_STOPPING
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Finish any in-progress recordings
        for ch in self.channels:
            if ch.is_recording and ch.buffer:
                await self._finish_recording(ch)
        # Release the pool consumer (tears down connection if refcount hits 0)
        if self.client:
            try:
                await connection_pool.release(self.device_id, self._consumer_id)
            except Exception as e:
                logger.warning(f"DeviceCapture release failed: {e}")
            self.client = None
        self._state = STATE_IDLE
        logger.info(f"📼 DeviceCapture stopped: job={self.job_id}")

    def pause(self) -> None:
        """Pause recording (called when a LIVE consumer claims the device).

        The capture loop keeps running (so it can resume instantly) but
        stops processing IQ / buffering audio.
        """
        if self._state == STATE_RUNNING:
            self._state = STATE_PAUSED
            if self.client and hasattr(self.client, 'flush'):
                self.client.flush()
            logger.info(f"⏸️ DeviceCapture paused: job={self.job_id}")

    def resume(self) -> None:
        """Resume recording after a pause."""
        if self._state == STATE_PAUSED:
            self._state = STATE_RUNNING
            logger.info(f"▶️ DeviceCapture resumed: job={self.job_id}")

    # ------------------------------------------------------------------
    # Preemption handling (single shared tuner)
    # ------------------------------------------------------------------
    def _on_preempt(self, new_id: Optional[str], new_prio: int,
                    old_id: Optional[str], old_prio: int) -> None:
        """Called by SharedDeviceConnection when the controller changes.

        - LIVE (interactive listening) claims the tuner → we PAUSE *unless*
          the tuner is already on OUR center frequency / sample rate. When a
          LIVE listener tunes via VFO offset (OpenWebRX-style) without
          retuning the hardware, our target channels are still reachable at
          the same offsets, so pausing would only silence the recording for
          no reason. We must finish in-progress recordings ONLY when the
          tuner actually moved to a different center frequency.
        - LIVE releases the tuner and we become controller again → we
          RESUME and RE-APPLY our tuner (freq/sample_rate/gain/ppm),
          because LIVE may have retuned/changed ppm. Without this the
          recorded frequency would be off and ppm would be the user's.
        """
        if not self._running:
            return
        # We are being preempted by a higher-priority consumer (LIVE).
        if new_id != self._consumer_id and new_prio >= PRIORITY_LIVE:
            # Check whether the shared tuner is actually moving away from
            # our center/sample_rate. If a LIVE listener is decoding via a
            # VFO offset on the SAME center, our channels are still valid
            # and we can keep recording.
            shared = self._shared_client
            same_tuner = (
                shared is not None
                and int(getattr(shared, '_frequency', 0) or 0) == self.center_hz
                and int(getattr(shared, '_sample_rate', 0) or 0) == self.sample_rate
            )
            if same_tuner:
                logger.info(
                    f"🎧 [job={self.job_id}] LIVE listener on same center "
                    f"({self.center_hz/1e6:.4f} MHz) — keep recording (no pause)"
                )
                # Stay RUNNING but remember LIVE owns the tuner now; the
                # stream loop will still process IQ for our channels as
                # long as they remain in the (unchanged) passband.
                return
            logger.info(
                f"⏸️ [job={self.job_id}] Preempted by {new_id} (prio {new_prio})"
            )
            self.pause()
            # Finish in-progress recordings on a best-effort basis (the tuner
            # just moved, so any further buffering would be the wrong freq).
            asyncio.ensure_future(self._finish_all_in_progress())
            return
        # Control has come back to us (LIVE left).
        if new_id == self._consumer_id and old_id != self._consumer_id:
            logger.info(
                f"▶️ [job={self.job_id}] Reclaimed tuner from {old_id}"
            )
            asyncio.ensure_future(self._reclaim_and_resume())

    async def _finish_all_in_progress(self) -> None:
        """Finish every in-progress channel recording (used on preempt)."""
        for ch in self.channels:
            try:
                if ch.is_recording and ch.buffer:
                    await self._finish_recording(ch)
            except Exception as e:
                logger.warning(f"Preempt finish error freq={ch.frequency_hz}: {e}")

    async def _reclaim_and_resume(self) -> None:
        """Re-apply our tuner settings, then resume processing IQ."""
        try:
            await self._reclaim_tuner()
        except Exception as e:
            logger.warning(f"Reclaim tuner failed for job {self.job_id}: {e}")
        # Only resume if we actually are the controller now (could have been
        # re-preempted in the meantime).
        if self._is_controller():
            self.resume()
        else:
            logger.info(
                f"⏸️ [job={self.job_id}] Still not controller after reclaim; "
                f"staying paused"
            )

    async def _reclaim_tuner(self) -> None:
        """Re-apply this job's full tuner state to the shared device.

        Called whenever we (re)gain control of the tuner. Restores the
        center frequency, sample rate, gain and PPM configured for this
        job window — which a LIVE consumer may have overwritten while it
        was listening elsewhere. This guarantees the recorded frequency
        is correct and uses the JOB's ppm, NOT the value the user set
        while listening live.

        NOTE: We intentionally do NOT bail out when we are not yet the
        controller. The first set_* call below triggers _claim_if_needed
        on the SharedDeviceConnection, which promotes us to controller if
        our priority (PRIORITY_JOB=5) outranks the current controller
        (e.g. an idle LIVE consumer at PRIORITY_IDLE=1). Without claiming
        here, a freshly started JOB would never take the tuner away from
        the boot-time autostart LIVE consumer, and would demodulate at
        the LIVE center frequency → "เสียงซ่าๆ".
        """
        if not self.client:
            return
        was_controller = self._is_controller()
        logger.info(
            f"🎛️ [job={self.job_id}] Reclaim tuner → "
            f"center={self.center_hz/1e6:.4f} MHz sr={self.sample_rate} "
            f"gain={self.gain} ppm={self.ppm} "
            f"(was_controller={was_controller})"
        )
        # These calls trigger _claim_if_needed on the SharedDeviceConnection,
        # which promotes us to controller if our priority outranks the
        # current controller (e.g. boot-time autostart LIVE @ PRIORITY_IDLE).
        # set_freq_correction re-applies set_frequency internally, so do ppm
        # first then freq/sr/gain to be explicit.
        await self.client.set_freq_correction(self.ppm)
        await self.client.set_frequency(self.center_hz)
        await self.client.set_sample_rate(self.sample_rate)
        await self.client.set_gain(self.gain)
        # Drop any stale IQ that was captured while LIVE owned the tuner so we
        # don't demodulate audio from the user's frequency on resume.
        try:
            self.client.flush()
        except Exception:
            pass
        # Reset per-channel demodulator state so filter transients from the
        # foreign center frequency don't bleed into the new recording.
        for ch in self.channels:
            ch.demod.reset()
            ch.demod.set_vfo_offset(ch.offset_hz)
        if not was_controller and self._is_controller():
            logger.info(
                f"🎛️ [job={self.job_id}] Claimed tuner (was not controller before)"
            )
        # Notify the LIVE side that we just retuned the SHARED tuner out from
        # under it. Without this, the LIVE consumer's mirror (freq/sr/ppm)
        # stays STALE and its VFO offset — relative to the OLD center — now
        # points outside the new passband. Symptom: dashboard shows a
        # "listening" frequency like 169.263 MHz while the real passband is
        # 168.000–169.024 MHz (off-screen cursor + pure-noise audio).
        try:
            from backend.sdr.manager import notify_live_tuner_changed
            await notify_live_tuner_changed(self.device_id)
        except Exception as e:
            logger.warning(
                f"[job={self.job_id}] notify_live_tuner_changed failed: {e}"
            )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def state(self) -> str:
        return self._state

    def runtime_status(self) -> dict:
        return {
            "job_id": self.job_id,
            "device_id": self.device_id,
            "center_hz": self.center_hz,
            "sample_rate": self.sample_rate,
            "connected": bool(self.client and self.client.connected),
            "state": self._state,
            "frequencies": [
                {
                    "frequency_hz": ch.frequency_hz,
                    "demod_mode": ch.demod_mode.value,
                    "bandwidth_hz": ch.bandwidth_hz,
                    "squelch_db": ch.squelch_db,
                    "auto_squelch": ch.auto_squelch,
                    "noise_floor_db": (round(ch.noise_floor_db, 1)
                                       if ch.noise_floor_db is not None else None),
                    "effective_squelch_db": round(ch.effective_squelch_db, 1),
                    "label": ch.label,
                    "is_recording": ch.is_recording,
                    "signal_level_db": round(ch.signal_level, 1),
                    "in_passband": ch.in_passband,
                }
                for ch in self.channels
            ],
        }

    async def _stream_loop(self) -> None:
        consecutive_errors = 0
        max_errors = 50
        try:
            while self._running and self.client and self.client.connected:
                try:
                    chunk = max(1024, int(self.sample_rate * 0.05))
                    iq = await self.client.read_samples(chunk * 2)
                    if not iq:
                        await asyncio.sleep(0.01)
                        continue
                    consecutive_errors = 0
                    # Skip processing when paused (LIVE consumer has control)
                    if self._state == STATE_PAUSED:
                        continue
                    await self._process_iq(iq)
                except asyncio.CancelledError:
                    break
                except Exception as inner:
                    consecutive_errors += 1
                    logger.warning(
                        f"DeviceCapture stream error ({consecutive_errors}/{max_errors}): {inner}"
                    )
                    if consecutive_errors >= max_errors:
                        logger.error("DeviceCapture: too many errors, stopping")
                        break
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DeviceCapture stream loop crashed: {e}", exc_info=True)
        finally:
            self._running = False

    async def _process_iq(self, iq_data: bytes) -> None:
        """Demodulate + apply squelch + buffer audio for each frequency channel.

        Even when a LIVE consumer has claimed control of the shared device
        (retuning it to a different center frequency), we keep reading the
        fanned-out IQ and try to reach each target frequency via a dynamic
        offset. Channels that fall outside the current passband are skipped
        (and any in-progress recording is finished to avoid splicing audio
        from a different frequency).
        """
        # Determine the ACTUAL tuner state of the SHARED device. We must read
        # from the underlying RtlTcpClient (self._shared_client), NOT from our
        # consumer's mirror (self.client._frequency) which goes stale the
        # moment a LIVE consumer retunes the shared device. Reading the stale
        # mirror was the root cause of "เสียงซ่าๆ": we demodulated at the
        # wrong offset because we thought the center was still ours.
        shared = self._shared_client
        if shared is not None:
            actual_center = int(getattr(shared, '_frequency', 0) or self.center_hz)
            actual_sr = int(getattr(shared, '_sample_rate', 0) or self.sample_rate)
        else:
            actual_center = self.center_hz
            actual_sr = self.sample_rate
        # Guard: if the shared client reported a None/0 sample_rate (e.g.
        # during RTL-TCP reconnect), fall back to our configured value.
        # Without this, compute_signal_level receives sample_rate=None →
        # "unsupported operand type(s) for /: 'NoneType' and 'NoneType'"
        # on every chunk, which silently breaks squelch and leaves signal_level
        # stuck at -120 dB.
        if not actual_sr or actual_sr <= 0:
            actual_sr = self.sample_rate

        # === DIAGNOSTIC: log actual tuner state + per-channel signal level
        # every ~5s so we can compare JOB-alone vs JOB+LIVE behavior.
        # Controlled by SDR_JOB_DEBUG env var (off by default). ===
        now = time.time()
        _debug = bool(getattr(settings, "SDR_JOB_DEBUG", False))
        if _debug and (not hasattr(self, "_diag_last") or now - self._diag_last > 5.0):
            self._diag_last = now
            iq_len = len(iq_data)
            ctrl = "?"
            try:
                if self.client and self.client._conn:
                    ctrl = str(self.client._conn._controller_id)
            except Exception:
                pass
            logger.info(
                f"🔎 [diag job={self.job_id}] state={self._state} "
                f"ctrl={ctrl} am_controller={self._is_controller()} "
                f"actual_center={actual_center/1e6:.4f}MHz want_center={self.center_hz/1e6:.4f}MHz "
                f"actual_sr={actual_sr} want_sr={self.sample_rate} "
                f"iq_bytes={iq_len} ppm(shared)={getattr(shared, '_ppm', '?')}"
            )
        # Safety: if the actual center/sample_rate changed from what our
        # demodulator was built for, the dynamic offset handling below will
        # recompute per-channel offsets; if a channel is unreachable we skip
        # it. This prevents recording audio at the wrong frequency.

        for ch in self.channels:
            try:
                # Check if this channel is still reachable given the actual
                # center frequency. If the device was retuned significantly,
                # our target may now be outside the passband.
                reachable = ch.update_offset(actual_center, actual_sr)
                if not reachable:
                    # Finish any recording that's in progress (the audio
                    # from the new center would be at the wrong frequency).
                    if ch.is_recording and ch.buffer:
                        logger.info(
                            f"📼 [job={self.job_id}] Finish {ch.frequency_hz/1e6:.4f} MHz "
                            f"(out of passband: center moved to {actual_center/1e6:.4f} MHz)"
                        )
                        await self._finish_recording(ch)
                    ch.signal_level = -120.0
                    continue

                # Snapshot the per-channel DSP params here on the event-loop
                # thread before offloading, so the worker reads a consistent
                # set of scalars even if config changes mid-chunk.
                dsp_mode = ch.demod_mode
                dsp_bw = ch.bandwidth_hz
                dsp_vfo = ch.offset_hz
                loop = asyncio.get_running_loop()
                # Run the two CPU-heavy DSP steps (signal-level ~21 ms +
                # demod ~15 ms at 2.4 MSPS) in PARALLEL off the event loop.
                # Previously both ran inline and, across N channels, consumed
                # N × ~36 ms per 50 ms chunk — pegging the process at 100% CPU
                # and starving every HTTP/WebSocket coroutine.
                level, audio = await asyncio.gather(
                    loop.run_in_executor(
                        self._dsp_executor,
                        compute_signal_level,
                        iq_data, actual_sr, dsp_mode, dsp_bw, dsp_vfo,
                    ),
                    loop.run_in_executor(
                        self._dsp_executor,
                        ch.demod.process,
                        iq_data, dsp_mode, actual_sr, self.audio_rate, dsp_bw,
                    ),
                )
                ch.signal_level = level
                # Adaptive squelch: feed the new level to the per-channel
                # estimator (frozen while a recording is active so a long
                # transmission can't pull the threshold up and clip itself).
                if ch.auto_estimator is not None:
                    ch.auto_estimator.update(level, frozen=ch.is_recording, now=now)
                # === DIAGNOSTIC (SDR_JOB_DEBUG): per-channel level vs squelch,
                # including a center-probe (offset=0) to isolate "no signal at
                # target" from a broken demod/signal-level function.
                # Also logs the AUTO-SQUELCH state (noise floor, effective
                # threshold, frozen) so we can see how the adaptive threshold
                # behaves under dense adjacent-channel traffic. ===
                if _debug and hasattr(self, "_diag_last") and abs(now - self._diag_last) < 0.2:
                    try:
                        level_center = compute_signal_level(
                            iq_data,
                            sample_rate=actual_sr,
                            mode=ch.demod_mode,
                            channel_bw=ch.bandwidth_hz,
                            vfo_offset_hz=0.0,
                        )
                    except Exception:
                        level_center = -999.0
                    # Spectrum sweep around the target offset to visualize
                    # adjacent-channel leakage (offsets relative to TARGET).
                    target_off = ch.offset_hz
                    sweep_parts = []
                    for rel_khz in (-25, -12.5, 0, 12.5, 25):
                        try:
                            sv = compute_signal_level(
                                iq_data,
                                sample_rate=actual_sr,
                                mode=ch.demod_mode,
                                channel_bw=ch.bandwidth_hz,
                                vfo_offset_hz=target_off + rel_khz * 1000.0,
                            )
                            mark = "*" if rel_khz == 0 else ""
                            sweep_parts.append(f"{rel_khz:+.1f}k={sv:.0f}{mark}")
                        except Exception:
                            pass
                    sweep_str = " ".join(sweep_parts)
                    # Auto-squelch state (if enabled)
                    is_auto = ch.auto_estimator is not None
                    if is_auto:
                        nf = ch.auto_estimator.noise_floor_db
                        ot = ch.auto_estimator.open_threshold_db
                        ct = ch.auto_estimator.close_threshold_db
                        frozen = ch.is_recording
                        sq_str = (f"AUTO floor={nf:.1f} open={ot:.1f} close={ct:.1f} "
                                  f"act={frozen}")
                    else:
                        sq_str = f"MANUAL sql={ch.squelch_db:.1f}"
                    print(
                        f"🔎 [probe] {ch.frequency_hz/1e6:.4f}MHz "
                        f"level={level:.1f}dB | center0={level_center:.1f}dB | "
                        f"{sq_str} | rec={ch.is_recording} "
                        f"| SWEEP(rel) {sweep_str}",
                        flush=True,
                    )

                # `audio` came from the offloaded demod.process in the gather
                # above. The rest of this loop body (squlech gating, buffering,
                # recording start/finish) runs on the event loop and is cheap.

                # === DIAG: audio RMS to confirm demod output quality ===
                if _debug and hasattr(self, "_diag_last") and abs(now - self._diag_last) < 0.2:
                    import numpy as _np
                    if len(audio) > 0:
                        audio_rms = float(_np.sqrt(_np.mean(audio.astype(_np.float64) ** 2)))
                        audio_max = float(_np.max(_np.abs(audio)))
                    else:
                        audio_rms = -1.0
                        audio_max = -1.0
                    print(
                        f"🔎 [audio] {ch.frequency_hz/1e6:.4f}MHz "
                        f"demod_offset_inside={ch.demod._vfo_offset_hz/1000:+.1f}kHz "
                        f"audio_rms={audio_rms:.4f} audio_max={audio_max:.4f} "
                        f"samples={len(audio)}",
                        flush=True,
                    )

                # Gate decision with HYSTERESIS.
                # - To OPEN (start recording): level must exceed the OPEN threshold.
                # - To CLOSE (begin silence countdown): level must fall below the
                #   CLOSE threshold. The band between open and close prevents the
                #   gate from chattering open/closed when the level oscillates
                #   around a single threshold — which previously reset the
                #   silence timer on every re-cross and produced over-long
                #   spliced-noise recordings under dense adjacent traffic.
                is_auto = ch.auto_estimator is not None
                if is_auto:
                    open_thr = ch.auto_estimator.open_threshold_db
                    close_thr = ch.auto_estimator.close_threshold_db
                else:
                    open_thr = ch.squelch_db
                    close_thr = ch.squelch_db

                if not ch.is_recording:
                    # Gate closed: open only when level rises above OPEN.
                    above = level > open_thr
                else:
                    # Gate open: stay open until level drops below CLOSE.
                    above = level > close_thr

                if above and not ch.is_recording:
                    ch.is_recording = True
                    ch.buffer = []
                    ch.start_time = time.time()
                    ch.silence_start = None
                    logger.info(
                        f"📼 [job={self.job_id}] Auto-rec START {ch.frequency_hz/1e6:.4f} MHz "
                        f"({level:.1f} dB, open_thr={open_thr:.1f})"
                    )
                elif above and ch.is_recording:
                    ch.silence_start = None
                    if len(audio) > 0:
                        ch.buffer.append(audio)
                elif not above and ch.is_recording:
                    if ch.silence_start is None:
                        ch.silence_start = time.time()
                        if len(audio) > 0:
                            ch.buffer.append(audio)
                    elif time.time() - ch.silence_start > self.silence_timeout:
                        await self._finish_recording(ch)

                if ch.is_recording and ch.start_time:
                    elapsed = time.time() - ch.start_time
                    if elapsed > self.max_recording_duration:
                        await self._finish_recording(ch)
            except Exception as e:
                logger.warning(
                    f"DeviceCapture process error freq={ch.frequency_hz}: {e}"
                )
                # If the channel is stuck "recording" because DSP errors
                # keep skipping the gate/silence logic, force-finish it so
                # it doesn't hold is_recording=True forever (the
                # "🔒อัดอยู่" stuck symptom). We only force-finish if the
                # recording has been running longer than max_recording_duration
                # OR silence_timeout has clearly elapsed.
                if ch.is_recording and ch.start_time:
                    elapsed = time.time() - ch.start_time
                    if elapsed > self.max_recording_duration:
                        logger.warning(
                            f"📼 [job={self.job_id}] Force-finish stuck recording "
                            f"{ch.frequency_hz/1e6:.4f} MHz (DSP error + elapsed {elapsed:.0f}s)"
                        )
                        await self._finish_recording(ch)

    async def _finish_recording(self, ch: _FrequencyChannel) -> None:
        """Save buffered audio to WAV + DB, then reset channel state."""
        if not ch.buffer:
            ch.is_recording = False
            ch.start_time = None
            ch.silence_start = None
            return

        audio_data = np.concatenate(ch.buffer)
        duration = len(audio_data) / self.audio_rate
        if duration < self.min_recording_duration:
            logger.info(
                f"📼 [job={self.job_id}] Discard short {ch.frequency_hz/1e6:.4f} MHz "
                f"({duration:.1f}s < {self.min_recording_duration}s)"
            )
            ch.is_recording = False
            ch.buffer = []
            ch.start_time = None
            ch.silence_start = None
            return

        await self._save_recording(ch, audio_data, duration)

        ch.is_recording = False
        ch.buffer = []
        ch.start_time = None
        ch.silence_start = None

    async def _save_recording(
        self, ch: _FrequencyChannel, audio_data: np.ndarray, duration: float
    ) -> None:
        """Write WAV + insert DB row + fire auto-transcribe."""
        recordings_dir = Path(settings.RECORDINGS_DIR)
        recordings_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        freq_hz = ch.frequency_hz
        filename = f"{ts}_{freq_hz}_{ch.demod_mode.value}_job{self.job_id[:8]}.wav"
        filepath = recordings_dir / filename
        sf.write(str(filepath), audio_data, self.audio_rate, subtype="PCM_16")

        # Capture file size on disk for storage usage aggregation.
        try:
            file_size = filepath.stat().st_size
        except Exception:
            file_size = None

        rec_id = str(uuid.uuid4())
        freq_mhz = freq_hz / 1e6
        now = datetime.now(timezone.utc)
        try:
            async with async_session() as session:
                rec = Recording(
                    id=rec_id,
                    frequency=freq_hz,
                    frequency_mhz=freq_mhz,
                    mode=ch.demod_mode,
                    signal_strength=round(ch.signal_level, 1),
                    audio_file_path=str(filepath),
                    audio_file_name=filename,
                    file_size=file_size,
                    duration=round(duration, 2),
                    transcript=None,
                    status=RecordingStatus.COMPLETED,
                    source="auto_job",
                    is_unread=1,
                    channel_bw=ch.bandwidth_hz,
                    device_id=self.device_id,
                    recording_job_id=self.job_id,
                    recorded_at=now,
                    created_at=now,
                )
                session.add(rec)
                await session.commit()
        except Exception as db_err:
            logger.error(f"DeviceCapture DB insert failed: {db_err}")

        logger.info(
            f"💾 [job={self.job_id}] Recording saved: {filename} "
            f"({duration:.1f}s, {ch.demod_mode.value}, {freq_mhz:.4f} MHz)"
        )

        # Auto-transcribe if enabled
        try:
            from backend.services.stt import transcribe, _get_stt_config
            cfg = await _get_stt_config()
            if cfg.get("auto_transcribe") and cfg.get("api_key"):
                async def _do():
                    try:
                        result = await transcribe(str(filepath))
                        text = (result.get("text") or "").strip()
                        lang = result.get("language", "th")
                        provider = result.get("provider", "")
                        model = result.get("model")
                        error = result.get("error")
                        stt_dur = result.get("duration")
                        async with async_session() as session:
                            db_rec = await session.get(Recording, rec_id)
                            if db_rec:
                                db_rec.transcript = text or "(ไม่สามารถถอดเสียงได้)"
                                db_rec.transcript_language = lang
                                db_rec.stt_provider = provider or None
                                db_rec.stt_model = model or None
                                db_rec.stt_error = error or None
                                db_rec.stt_duration = stt_dur
                                db_rec.status = RecordingStatus.COMPLETED
                                await session.commit()
                        logger.info(
                            f"✅ [job={self.job_id}] Auto-transcribed {rec_id}: "
                            f"{len(text)} chars"
                        )
                        # Enqueue keyword-alert evaluation (non-blocking).
                        try:
                            from backend.alerts.jobs import enqueue_alert_job
                            await enqueue_alert_job(rec_id)
                        except Exception as e:
                            logger.warning(f"Alert enqueue failed for {rec_id}: {e}")
                    except Exception as e:
                        logger.error(f"Auto-transcribe failed for {rec_id}: {e}")
                asyncio.create_task(_do())
        except Exception as e:
            logger.warning(f"Auto-transcribe check failed: {e}")