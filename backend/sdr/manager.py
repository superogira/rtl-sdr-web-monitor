"""
SDR Manager - orchestrates RTL-TCP connection, demodulation, and recording.
"""
import asyncio
import json
import time
import logging
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import soundfile as sf

from backend.config import settings
from backend.sdr.rtl_tcp import RtlTcpClient
from backend.audio.demod import (
    DemodMode, demodulate, compute_signal_level, StreamingDemodulator,
)
from backend.audio.broadcaster import audio_broadcaster
from backend.database import async_session
from backend.models.recording import Recording, RecordingStatus
from backend.services.app_settings import get_setting
from backend.services.stt import transcribe, _get_stt_config
from sqlalchemy import select

logger = logging.getLogger(__name__)

# Persisted SDR settings (host/port/freq/etc.) so UI changes survive restarts.
SDR_SETTINGS_FILE = Path("data/sdr_settings.json")
# Default values mirroring settings.* so a missing file still works.
_DEFAULT_SDR_SETTINGS = {
    "host": settings.RTL_TCP_HOST,
    "port": settings.RTL_TCP_PORT,
    "frequency": settings.RTL_TCP_CENTER_FREQ,
    "sample_rate": settings.RTL_TCP_SAMPLE_RATE,
    "gain": settings.RTL_TCP_GAIN,
    "freq_correction_ppm": settings.RTL_TCP_FREQ_CORRECTION_PPM,
}


class SDRManager:
    """Manages SDR operations: connection, streaming, demodulation, recording.

    Recording is dual-track: auto and manual recordings are fully independent.
    Both can run simultaneously with separate buffers.
    """

    # Channel bandwidth (Hz) — user-adjustable via /api/sdr/bandwidth.
    # Default 12kHz = NBFM voice. Wider values for WBFM/broadcast.
    _DEFAULT_CHANNEL_BW = 12000

    def __init__(self):
        persisted = self._load_persisted_settings()
        self.client = RtlTcpClient(host=persisted["host"], port=persisted["port"])
        # Apply persisted frequency/sample_rate/gain as initial device values.
        self.client._frequency = persisted["frequency"]
        self.client._sample_rate = persisted["sample_rate"]
        self.client._gain = persisted["gain"]
        self.client._ppm = persisted["freq_correction_ppm"]
        self.mode = DemodMode.FM
        self.audio_rate = settings.AUDIO_SAMPLE_RATE
        self.is_streaming = False

        # === Dual-track recording: auto and manual are INDEPENDENT ===
        # Both can record at the same time with separate buffers.
        self._auto_recording = False
        self._manual_recording = False
        self._auto_buffer: list[np.ndarray] = []
        self._manual_buffer: list[np.ndarray] = []
        self._auto_start_time: Optional[float] = None
        self._manual_start_time: Optional[float] = None
        self._auto_silence_start: Optional[float] = None
        self._auto_record = True

        self._audio_buffer: list[np.ndarray] = []
        self._signal_level = -120.0
        self._squelch_level = settings.SQUELCH_LEVEL
        self._last_recording_path: Optional[str] = None
        self._new_recordings: list[dict] = []
        self._callbacks: list = []
        self._spectrum_callbacks: list = []
        self._last_spectrum: list = []
        self._spectrum_prev: Optional[np.ndarray] = None  # For exponential averaging
        self._spectrum_alpha: float = 0.2  # EMA factor (lower = smoother, less noise)
        self._fft_bins: int = 1024  # FFT output bins (configurable: 256-8192, power of 2)
        self._channel_bw: int = self._DEFAULT_CHANNEL_BW
        self._stream_task: Optional[asyncio.Task] = None
        # Stateful streaming demodulator — carries filter state across chunks
        # to eliminate per-chunk boundary clicks (~20 Hz buzz) in live audio
        # and recordings. Reset whenever mode/sample_rate/audio_rate/channel_bw
        # (NBFM↔WBFM) changes.
        # StreamingDemodulator self-detects mode/sample_rate/audio_rate/NBFM↔WBFM
        # changes and resets its filter state internally, so no extra tracking
        # fields are needed here.
        self._demod = StreamingDemodulator()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def signal_level(self) -> float:
        return self._signal_level

    @property
    def is_recording(self) -> bool:
        """True if EITHER auto or manual recording is active (read-only)."""
        return self._auto_recording or self._manual_recording

    @property
    def recording_source(self) -> Optional[str]:
        """What is currently recording: 'auto', 'manual', 'both', or None."""
        a, m = self._auto_recording, self._manual_recording
        if a and m:
            return "both"
        if m:
            return "manual"
        if a:
            return "auto"
        return None

    @property
    def status(self) -> dict:
        return {
            **self.client.get_status(),
            "mode": self.mode.value,
            "streaming": self.is_streaming,
            "recording": self.is_recording,
            "recording_source": self.recording_source,
            "manual_recording": self._manual_recording,
            "auto_recording": self._auto_recording,
            "signal_level_db": round(self._signal_level, 1),
            "squelch_level": self._squelch_level,
            "auto_record": self._auto_record,
            "fft_bins": self._fft_bins,
            "channel_bw": self._channel_bw,
            "listeners": audio_broadcaster.listener_count,
        }

    # ------------------------------------------------------------------
    # Persisted settings (host/port/freq/etc. survive restarts)
    # ------------------------------------------------------------------
    @staticmethod
    def _load_persisted_settings() -> dict:
        """Load SDR settings from data/sdr_settings.json, falling back to defaults."""
        data = dict(_DEFAULT_SDR_SETTINGS)
        try:
            if SDR_SETTINGS_FILE.exists():
                with open(SDR_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # Only accept keys we know; coerce types defensively.
                if isinstance(saved.get("host"), str) and saved["host"].strip():
                    data["host"] = saved["host"].strip()
                if isinstance(saved.get("port"), (int, str)):
                    data["port"] = int(saved["port"])
                if isinstance(saved.get("frequency"), (int, str)):
                    data["frequency"] = int(saved["frequency"])
                if isinstance(saved.get("sample_rate"), (int, str)):
                    data["sample_rate"] = int(saved["sample_rate"])
                if isinstance(saved.get("gain"), (int, str)):
                    data["gain"] = int(saved["gain"])
                if isinstance(saved.get("freq_correction_ppm"), (int, str)):
                    data["freq_correction_ppm"] = int(saved["freq_correction_ppm"])
        except Exception as e:
            logger.warning(f"Failed to load SDR settings file, using defaults: {e}")
        return data

    def _save_persisted_settings(self) -> None:
        """Persist current SDR connection + tuner settings to disk."""
        try:
            SDR_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "host": self.client.host,
                "port": self.client.port,
                "frequency": self.client._frequency,
                "sample_rate": self.client._sample_rate,
                "gain": self.client._gain,
                "freq_correction_ppm": self.client._ppm,
            }
            with open(SDR_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist SDR settings: {e}")

    def set_connection(self, host: str, port: int) -> dict:
        """Update RTL-TCP target host/port and persist to disk."""
        self.client.set_connection(host, port)
        self._save_persisted_settings()
        logger.info(f"RTL-TCP target set to {host}:{port}")
        return {"host": self.client.host, "port": self.client.port}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> dict:
        """Connect to RTL-TCP server."""
        if self.is_streaming:
            await self.stop_streaming()
        if self.client.connected:
            await self.client.disconnect()
            await asyncio.sleep(0.5)

        success = await self.client.connect()
        self._save_persisted_settings()
        return {"success": success, "status": self.status}

    async def disconnect(self):
        """Disconnect from RTL-TCP."""
        await self.stop_streaming()
        await self.client.disconnect()

    # FM broadcast band (WBFM) — auto-select wide channel bandwidth when tuning here.
    WBFM_BANDS = [(65_800_000, 74_000_000), (76_000_000, 95_000_000), (87_500_000, 108_000_000)]
    WBFM_CHANNEL_BW = 200_000

    def _is_wbfm_band(self, freq_hz: int) -> bool:
        return any(lo <= freq_hz <= hi for lo, hi in self.WBFM_BANDS)

    async def set_frequency(self, freq_hz: int):
        """Set center frequency (auto-adjusts BW for FM broadcast band)."""
        await self.client.set_frequency(freq_hz)
        if self.mode == DemodMode.FM:
            if self._is_wbfm_band(freq_hz) and self._channel_bw != self.WBFM_CHANNEL_BW:
                self._channel_bw = self.WBFM_CHANNEL_BW
                logger.info(f"📻 WBFM band detected ({freq_hz/1e6:.1f} MHz) → "
                            f"channel BW auto-set to {self.WBFM_CHANNEL_BW//1000} kHz")
            elif not self._is_wbfm_band(freq_hz) and self._channel_bw == self.WBFM_CHANNEL_BW:
                self._channel_bw = self._DEFAULT_CHANNEL_BW
                logger.info(f"📻 Left WBFM band → channel BW restored to "
                            f"{self._DEFAULT_CHANNEL_BW//1000} kHz (NBFM)")
        self._save_persisted_settings()

    async def set_sample_rate(self, rate: int):
        await self.client.set_sample_rate(rate)
        self._save_persisted_settings()

    async def set_gain(self, gain_db: int):
        await self.client.set_gain(gain_db)
        self._save_persisted_settings()

    async def set_freq_correction(self, ppm: int):
        await self.client.set_freq_correction(ppm)
        self._save_persisted_settings()

    def set_channel_bw(self, bw_hz: int):
        if not (1000 <= bw_hz <= 500000):
            raise ValueError(f"Invalid channel BW: {bw_hz}. Must be 1kHz–500kHz")
        self._channel_bw = bw_hz
        logger.info(f"Channel BW set to {bw_hz} Hz ({bw_hz/1e3:.0f} kHz)")

    async def set_mode(self, mode: str):
        self.mode = DemodMode(mode.upper())

    def set_squelch(self, level_db: float):
        self._squelch_level = level_db

    def set_fft_bins(self, bins: int):
        valid_bins = [256, 512, 1024, 2048, 4096, 8192]
        if bins not in valid_bins:
            raise ValueError(f"Invalid FFT bins: {bins}. Must be one of {valid_bins}")
        self._fft_bins = bins
        self._spectrum_prev = None
        logger.info(f"FFT bins set to {bins}")

    async def start_streaming(self):
        if self.is_streaming:
            return {"error": "Already streaming"}
        if not self.client.connected:
            return {"error": "Not connected to RTL-TCP"}
        self.is_streaming = True
        self._stream_task = asyncio.create_task(self._stream_loop())
        return {"streaming": True}

    async def stop_streaming(self):
        """Stop streaming and finish any ongoing recordings."""
        self.is_streaming = False
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        # Finish both auto and manual recordings
        if self._auto_recording:
            await self._finish_auto_recording()
        if self._manual_recording:
            await self._finish_manual_recording()
        return {"streaming": False}

    # ------------------------------------------------------------------
    # Manual recording (user-controlled, independent from auto)
    # ------------------------------------------------------------------
    async def start_recording(self):
        """Start manual recording. Works independently from auto-record."""
        if not self.is_streaming:
            return {"error": "กรุณากด รับสัญญาณ ก่อนบันทึก"}
        if self._manual_recording:
            return {"error": "Already recording (manual)"}
        self._manual_recording = True
        self._manual_buffer = []
        self._manual_start_time = time.time()
        logger.info(f"🎙️ Manual recording started ({self.client._frequency/1e6:.3f} MHz)"
                    f" | auto_recording={self._auto_recording}")
        return {"recording": True, "source": "manual"}

    async def stop_recording(self):
        """Stop manual recording only (auto continues if active)."""
        if not self._manual_recording:
            return {"error": "No manual recording in progress"}
        # If buffer is empty (e.g. no audio received yet), reset state silently
        if not self._manual_buffer:
            self._manual_recording = False
            self._manual_start_time = None
            return {"recording": False, "source": "manual", "note": "No audio captured"}
        return await self._finish_manual_recording()

    def get_new_recordings(self) -> list[dict]:
        recordings = self._new_recordings.copy()
        self._new_recordings.clear()
        return recordings

    def on_new_recording(self, callback):
        self._callbacks.append(callback)

    def on_spectrum(self, callback):
        self._spectrum_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Main streaming loop — dual-track recording
    # ------------------------------------------------------------------
    async def _stream_loop(self):
        logger.info("Streaming started")
        consecutive_errors = 0
        max_errors = 50
        _spectrum_counter = 0
        _SPECTRUM_INTERVAL = 2
        try:
            while self.is_streaming and self.client.connected:
                try:
                    samples_per_chunk = max(1024, int(self.client._sample_rate * 0.05))
                    iq_data = await self.client.read_samples(samples_per_chunk * 2)
                    if not iq_data:
                        await asyncio.sleep(0.01)
                        continue

                    consecutive_errors = 0

                    self._signal_level = compute_signal_level(
                        iq_data,
                        sample_rate=self.client._sample_rate,
                        mode=self.mode,
                        channel_bw=self._channel_bw,
                    )

                    sample_rate = self.client._sample_rate
                    # Use the stateful streaming demodulator: it carries filter
                    # state (deemphasis zi, voice-band SOS zi, FM phase, SSB LO
                    # phase) across chunks, eliminating the ~20 Hz per-chunk
                    # boundary clicks that the stateless demodulate() produced.
                    audio = self._demod.process(
                        iq_data, self.mode, sample_rate, self.audio_rate,
                        channel_bw=self._channel_bw,
                    )

                    if len(audio) > 0:
                        self._audio_buffer.append(audio)
                        if len(self._audio_buffer) > 100:
                            self._audio_buffer = self._audio_buffer[-50:]
                        if audio_broadcaster.has_listeners:
                            await audio_broadcaster.broadcast(audio, self.audio_rate)

                    _spectrum_counter += 1
                    if _spectrum_counter >= _SPECTRUM_INTERVAL:
                        _spectrum_counter = 0
                        self._compute_spectrum(iq_data)

                    # === TRACK 1: Manual recording (independent) ===
                    if self._manual_recording:
                        if len(audio) > 0:
                            self._manual_buffer.append(audio)
                        if self._manual_start_time:
                            elapsed = time.time() - self._manual_start_time
                            if elapsed > settings.MAX_RECORDING_DURATION:
                                logger.info("Manual recording hit MAX_RECORDING_DURATION, finishing")
                                await self._finish_manual_recording()

                    # === TRACK 2: Auto recording (independent) ===
                    if self._auto_record and self.is_streaming:
                        above_squelch = self._signal_level > self._squelch_level

                        if above_squelch and not self._auto_recording:
                            # Signal detected → start auto recording
                            self._auto_recording = True
                            self._auto_buffer = []
                            self._auto_start_time = time.time()
                            self._auto_silence_start = None
                            logger.info(f"🎙️ Auto recording started (signal: {self._signal_level:.1f} dB)"
                                        f" | manual_recording={self._manual_recording}")
                        elif above_squelch and self._auto_recording:
                            self._auto_silence_start = None
                            if len(audio) > 0:
                                self._auto_buffer.append(audio)
                        elif not above_squelch and self._auto_recording:
                            if self._auto_silence_start is None:
                                self._auto_silence_start = time.time()
                                if len(audio) > 0:
                                    self._auto_buffer.append(audio)
                            elif time.time() - self._auto_silence_start > settings.VAD_SILENCE_TIMEOUT:
                                await self._finish_auto_recording()

                        if self._auto_recording and self._auto_start_time:
                            elapsed = time.time() - self._auto_start_time
                            if elapsed > settings.MAX_RECORDING_DURATION:
                                await self._finish_auto_recording()

                except Exception as inner_e:
                    consecutive_errors += 1
                    logger.warning(f"Stream iteration error ({consecutive_errors}/{max_errors}): {inner_e}")
                    if consecutive_errors >= max_errors:
                        logger.error("Too many errors, stopping stream")
                        break
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info("Streaming stopped")
        except Exception as e:
            logger.error(f"Stream error: {e}")
        finally:
            self.is_streaming = False
            logger.info("Stream loop exited")

    def _compute_spectrum(self, iq_data: bytes):
        """Compute FFT spectrum from IQ data and notify callbacks."""
        try:
            raw = np.frombuffer(iq_data, dtype=np.uint8)
            if len(raw) < 1024:
                return

            iq = (raw.astype(np.float32) - 127.5) / 127.5
            complex_data = iq[0::2] + 1j * iq[1::2]

            if len(complex_data) < 512:
                return

            fft_size = min(4096, len(complex_data))
            window = np.blackman(fft_size)
            windowed = complex_data[:fft_size] * window
            fft_result = np.fft.fftshift(np.abs(np.fft.fft(windowed)) / fft_size)
            fft_db = 20 * np.log10(fft_result + 1e-10)

            if self._spectrum_prev is not None and len(self._spectrum_prev) == len(fft_db):
                fft_db = self._spectrum_alpha * fft_db + (1 - self._spectrum_alpha) * self._spectrum_prev
            self._spectrum_prev = fft_db.copy()

            target_bins = self._fft_bins
            num_bins = min(target_bins, len(fft_db))
            indices = np.linspace(0, len(fft_db) - 1, num_bins, dtype=int)
            spectrum = fft_db[indices]

            if len(spectrum) < target_bins:
                spectrum = np.pad(spectrum, (0, target_bins - len(spectrum)), mode='edge')

            import base64
            spectrum_int16 = np.clip(spectrum, -32768, 32767).astype(np.int16)
            spectrum_b64 = base64.b64encode(spectrum_int16.tobytes()).decode('ascii')
            self._last_spectrum = spectrum_b64

            for cb in self._spectrum_callbacks:
                try:
                    cb(spectrum_b64)
                except Exception as e:
                    logger.error(f"Spectrum callback error: {e}")
        except Exception as e:
            logger.error(f"FFT error: {e}")

    # ------------------------------------------------------------------
    # Finish recordings — separate for auto and manual
    # ------------------------------------------------------------------
    async def _finish_auto_recording(self) -> Optional[dict]:
        """Finish auto recording and save to file."""
        if not self._auto_buffer:
            self._auto_recording = False
            self._auto_start_time = None
            self._auto_silence_start = None
            return {"error": "No audio data"}

        audio_data = np.concatenate(self._auto_buffer)
        duration = len(audio_data) / self.audio_rate

        if duration < settings.VAD_SILENCE_TIMEOUT:
            logger.info(f"Auto recording too short ({duration:.1f}s), discarding")
            self._auto_recording = False
            self._auto_buffer = []
            self._auto_start_time = None
            self._auto_silence_start = None
            return {"error": "Recording too short"}

        return await self._save_recording(audio_data, duration, source="auto")

    async def _finish_manual_recording(self) -> Optional[dict]:
        """Finish manual recording and save to file."""
        if not self._manual_buffer:
            self._manual_recording = False
            self._manual_start_time = None
            return {"error": "No audio data"}

        audio_data = np.concatenate(self._manual_buffer)
        duration = len(audio_data) / self.audio_rate

        result = await self._save_recording(audio_data, duration, source="manual")

        # Reset manual state regardless of save result
        self._manual_recording = False
        self._manual_buffer = []
        self._manual_start_time = None
        return result

    async def _save_recording(self, audio_data: np.ndarray, duration: float, source: str) -> dict:
        """Save audio data to WAV file, insert into DB, and notify callbacks."""
        recordings_dir = Path(settings.RECORDINGS_DIR)
        recordings_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{self.client._frequency}_{self.mode.value}.wav"
        filepath = recordings_dir / filename

        sf.write(str(filepath), audio_data, self.audio_rate, subtype="PCM_16")

        rec_id = str(uuid.uuid4())
        freq_hz = self.client._frequency
        freq_mhz = freq_hz / 1e6
        now = datetime.now(timezone.utc)

        # === INSERT into database ===
        try:
            async with async_session() as session:
                rec = Recording(
                    id=rec_id,
                    frequency=freq_hz,
                    frequency_mhz=freq_mhz,
                    mode=self.mode,
                    signal_strength=round(self._signal_level, 1),
                    audio_file_path=str(filepath),
                    audio_file_name=filename,
                    duration=round(duration, 2),
                    transcript=None,
                    status=RecordingStatus.COMPLETED,
                    source=source,
                    is_unread=1,
                    recorded_at=now,
                    created_at=now,
                )
                session.add(rec)
                await session.commit()
                logger.info(f"💾 Recording saved to DB: {rec_id}")
        except Exception as db_err:
            logger.error(f"DB insert failed (file still saved): {db_err}")

        recording_info = {
            "id": rec_id,
            "filename": filename,
            "filepath": str(filepath),
            "url": f"/recordings/{filename}",
            "duration": round(duration, 2),
            "frequency": freq_hz,
            "frequency_mhz": freq_mhz,
            "mode": self.mode.value,
            "signal_level": round(self._signal_level, 1),
            "signal_strength": round(self._signal_level, 1),
            "timestamp": now.isoformat(),
            "recorded_at": now.isoformat(),
            "transcription": None,
            "transcript": None,
            "source": source,
            "status": "completed",
            "is_unread": 1,
        }

        self._last_recording_path = str(filepath)
        self._new_recordings.append(recording_info)

        logger.info(
            f"✅ Recording saved [{source}]: {filename} ({duration:.1f}s, "
            f"{self.mode.value}, {freq_mhz:.3f} MHz)"
        )

        # Reset auto state if this was an auto recording
        if source == "auto":
            self._auto_recording = False
            self._auto_buffer = []
            self._auto_start_time = None
            self._auto_silence_start = None

        # Notify callbacks
        for cb in self._callbacks:
            try:
                await cb(recording_info)
            except Exception as e:
                logger.error(f"Callback error: {e}")

        # Auto-transcribe if enabled in global settings
        try:
            cfg = await _get_stt_config()
            if cfg.get("auto_transcribe") and cfg.get("api_key"):
                logger.info(f"🔄 Auto-transcribing recording {rec_id}...")
                rec_info_copy = dict(recording_info)

                async def _do_transcribe():
                    try:
                        result = await transcribe(str(filepath))
                        text = (result.get("text") or "").strip()
                        lang = result.get("language", "th")
                        provider = result.get("provider", "")
                        error = result.get("error")
                        async with async_session() as session:
                            db_rec = await session.get(Recording, rec_id)
                            if db_rec:
                                db_rec.transcript = text or "(ไม่สามารถถอดเสียงได้)"
                                db_rec.transcript_language = lang
                                db_rec.stt_provider = provider or None
                                db_rec.stt_error = error or None
                                db_rec.status = RecordingStatus.COMPLETED
                                await session.commit()
                        rec_info_copy["transcript"] = text
                        rec_info_copy["transcription"] = text
                        rec_info_copy["stt_provider"] = provider
                        rec_info_copy["stt_error"] = error
                        self._new_recordings.append({**rec_info_copy, "_transcribed": True})
                        logger.info(f"✅ Auto-transcribed {rec_id}: {len(text)} chars (provider={provider})")
                    except Exception as e:
                        logger.error(f"Auto-transcribe failed for {rec_id}: {e}")
                        # Store error in DB so user can see what went wrong
                        try:
                            async with async_session() as session:
                                db_rec = await session.get(Recording, rec_id)
                                if db_rec:
                                    db_rec.stt_error = str(e)
                                    db_rec.status = RecordingStatus.FAILED
                                    await session.commit()
                        except Exception:
                            pass

                asyncio.create_task(_do_transcribe())
        except Exception as e:
            logger.warning(f"Auto-transcribe check failed: {e}")

        return recording_info


# Global SDR manager instance
sdr_manager = SDRManager()