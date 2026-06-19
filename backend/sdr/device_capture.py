"""
DeviceCapture — background IQ capture + per-frequency recording for one SDR device.

Each DeviceCapture owns a dedicated RtlTcpClient (independent from the live
SDRManager used for interactive listening). It connects to the device's
rtl_tcp endpoint, tunes to the window's center frequency
((start_freq + sample_rate) / 2), then continuously reads IQ samples.

For each target frequency (which must lie within [start_freq, start_freq +
sample_rate]) it:
  - demodulates at offset (freq - center) using the configured mode/bandwidth
  - computes signal level within that bandwidth
  - applies squelch: starts buffering when signal > squelch, finishes and
    saves a WAV when signal drops below squelch for `silence_timeout` seconds
    (or hits `max_recording_duration`).

This module is used by JobScheduler (backend/sdr/job_scheduler.py) to run
recording jobs in the background without interfering with interactive use.
"""
import asyncio
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
from backend.sdr.connection_pool import pool as connection_pool, PRIORITY_JOB
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


class _FrequencyChannel:
    """Per-target-frequency state inside a DeviceCapture."""

    def __init__(self, freq: RecordingJobFrequency, center_hz: int, audio_rate: int):
        self.frequency_hz = freq.frequency_hz
        self.offset_hz = float(freq.frequency_hz - center_hz)
        self.squelch_db = float(freq.squelch_db)
        self.demod_mode = DemodMode(freq.demod_mode.upper())
        self.bandwidth_hz = int(freq.bandwidth_hz)
        self.label = freq.label
        self.audio_rate = audio_rate
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

    def update_offset(self, actual_center_hz: int, sample_rate: int) -> bool:
        """Recompute offset from the ACTUAL center frequency.

        Returns True if the channel is within the usable passband.
        When the LIVE consumer retunes the shared device, our intended
        center may differ — but our target frequency may still be
        reachable at a different offset. If it moved significantly during
        a recording, the caller should finish that recording to avoid
        splicing audio from different frequencies.
        """
        max_offset = sample_rate * 0.45  # 45% margin to avoid edge aliasing
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

        # Build frequency channels
        self.channels: List[_FrequencyChannel] = [
            _FrequencyChannel(f, self.center_hz, self.audio_rate)
            for f in (window.frequencies or [])
        ]

        # Resolve device host/port from the SdrDevice row
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self.client = None  # SharedConnectionConsumer (from pool)
        self._consumer_id: str = f"job:{self.job_id}"
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._state: str = STATE_IDLE

    async def _resolve_device_addr(self) -> None:
        """Load host/port from the SdrDevice DB row."""
        from backend.models.sdr_device import SdrDevice
        async with async_session() as session:
            dev = await session.get(SdrDevice, self.device_id)
            if dev is None:
                raise RuntimeError(f"Device {self.device_id} not found")
            cfg = dev.get_config()
            self._host = str(cfg.get("host") or settings.RTL_TCP_HOST).strip()
            self._port = int(cfg.get("port") or settings.RTL_TCP_PORT)

    async def start(self) -> None:
        """Connect via the shared pool and start the background capture loop."""
        if self._running:
            return
        await self._resolve_device_addr()
        # Acquire a shared consumer with JOB priority (yields to LIVE)
        self.client = await connection_pool.acquire(
            self.device_id, self._host, self._port,
            consumer_id=self._consumer_id, priority=PRIORITY_JOB,
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
        self._running = True
        self._state = STATE_RUNNING
        self._task = asyncio.create_task(self._stream_loop())
        logger.info(
            f"📼 DeviceCapture started: job={self.job_id} device={self.device_id} "
            f"center={self.center_hz/1e6:.4f} MHz sr={self.sample_rate} "
            f"{len(self.channels)} freq(s)"
        )

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
                    "label": ch.label,
                    "is_recording": ch.is_recording,
                    "signal_level_db": round(ch.signal_level, 1),
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
        # Determine the ACTUAL center frequency currently set on the device
        # (may differ from our intended center if a LIVE consumer retuned).
        actual_center = getattr(self.client, '_frequency', None) or self.center_hz
        actual_sr = getattr(self.client, '_sample_rate', None) or self.sample_rate

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

                level = compute_signal_level(
                    iq_data,
                    sample_rate=actual_sr,
                    mode=ch.demod_mode,
                    channel_bw=ch.bandwidth_hz,
                    vfo_offset_hz=ch.offset_hz,
                )
                ch.signal_level = level

                audio = ch.demod.process(
                    iq_data, ch.demod_mode, actual_sr, self.audio_rate,
                    channel_bw=ch.bandwidth_hz,
                )

                above = level > ch.squelch_db

                if above and not ch.is_recording:
                    ch.is_recording = True
                    ch.buffer = []
                    ch.start_time = time.time()
                    ch.silence_start = None
                    logger.info(
                        f"📼 [job={self.job_id}] Auto-rec START {ch.frequency_hz/1e6:.4f} MHz "
                        f"({level:.1f} dB)"
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
                    duration=round(duration, 2),
                    transcript=None,
                    status=RecordingStatus.COMPLETED,
                    source="auto_job",
                    is_unread=1,
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
                    except Exception as e:
                        logger.error(f"Auto-transcribe failed for {rec_id}: {e}")
                asyncio.create_task(_do())
        except Exception as e:
            logger.warning(f"Auto-transcribe check failed: {e}")