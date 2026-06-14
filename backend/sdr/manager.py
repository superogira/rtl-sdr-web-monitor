"""
SDR Manager - orchestrates RTL-TCP connection, demodulation, and recording.
"""
import asyncio
import json
import time
import logging
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np
import soundfile as sf

from backend.config import settings
from backend.sdr.rtl_tcp import RtlTcpClient
from backend.audio.demod import DemodMode, demodulate, compute_signal_level
from backend.audio.broadcaster import audio_broadcaster

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
}


class SDRManager:
    """Manages SDR operations: connection, streaming, demodulation, recording."""

    def __init__(self):
        persisted = self._load_persisted_settings()
        self.client = RtlTcpClient(host=persisted["host"], port=persisted["port"])
        # Apply persisted frequency/sample_rate/gain as initial device values.
        self.client._frequency = persisted["frequency"]
        self.client._sample_rate = persisted["sample_rate"]
        self.client._gain = persisted["gain"]
        self.mode = DemodMode.FM
        self.audio_rate = settings.AUDIO_SAMPLE_RATE
        self.is_streaming = False
        self.is_recording = False
        self._stream_task: Optional[asyncio.Task] = None
        self._audio_buffer: list[np.ndarray] = []
        self._recording_buffer: list[np.ndarray] = []
        self._signal_level = -120.0
        self._squelch_level = settings.SQUELCH_LEVEL
        self._auto_record = True
        self._last_recording_path: Optional[str] = None
        self._recording_start_time: Optional[float] = None
        self._silence_start: Optional[float] = None
        self._new_recordings: list[dict] = []
        self._callbacks: list = []
        self._spectrum_callbacks: list = []
        self._last_spectrum: list = []
        self._spectrum_prev: Optional[np.ndarray] = None  # For exponential averaging
        self._spectrum_alpha: float = 0.2  # EMA factor (lower = smoother, less noise)
        self._fft_bins: int = 1024  # FFT output bins (configurable: 256-8192, power of 2)

    @property
    def signal_level(self) -> float:
        return self._signal_level

    @property
    def status(self) -> dict:
        return {
            **self.client.get_status(),
            "mode": self.mode.value,
            "streaming": self.is_streaming,
            "recording": self.is_recording,
            "signal_level_db": round(self._signal_level, 1),
            "squelch_level": self._squelch_level,
            "auto_record": self._auto_record,
            "fft_bins": self._fft_bins,
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
            }
            with open(SDR_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist SDR settings: {e}")

    def set_connection(self, host: str, port: int) -> dict:
        """Update RTL-TCP target host/port and persist to disk.

        Takes effect on the next connect(). Caller should disconnect first if
        currently connected and wants the new endpoint immediately.
        """
        self.client.set_connection(host, port)
        self._save_persisted_settings()
        logger.info(f"RTL-TCP target set to {host}:{port}")
        return {"host": self.client.host, "port": self.client.port}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> dict:
        """Connect to RTL-TCP server."""
        # Cleanup any existing connection first
        if self.is_streaming:
            await self.stop_streaming()
        if self.client.connected:
            await self.client.disconnect()
            await asyncio.sleep(0.5)  # Give RTL-TCP time to cleanup

        success = await self.client.connect()
        # Persist latest values (host/port/freq/etc. used for this attempt).
        self._save_persisted_settings()
        return {
            "success": success,
            "status": self.status,
        }

    async def disconnect(self):
        """Disconnect from RTL-TCP."""
        await self.stop_streaming()
        await self.client.disconnect()

    async def set_frequency(self, freq_hz: int):
        """Set center frequency."""
        await self.client.set_frequency(freq_hz)
        self._save_persisted_settings()

    async def set_sample_rate(self, rate: int):
        """Set sample rate."""
        await self.client.set_sample_rate(rate)
        self._save_persisted_settings()

    async def set_gain(self, gain_db: int):
        """Set gain."""
        await self.client.set_gain(gain_db)
        self._save_persisted_settings()

    async def set_mode(self, mode: str):
        """Set demodulation mode."""
        self.mode = DemodMode(mode.upper())

    def set_squelch(self, level_db: float):
        """Set squelch level."""
        self._squelch_level = level_db

    def set_fft_bins(self, bins: int):
        """Set FFT output bins (must be power of 2, range 256-8192)."""
        valid_bins = [256, 512, 1024, 2048, 4096, 8192]
        if bins not in valid_bins:
            raise ValueError(f"Invalid FFT bins: {bins}. Must be one of {valid_bins}")
        self._fft_bins = bins
        self._spectrum_prev = None
        logger.info(f"FFT bins set to {bins}")

    async def start_streaming(self):
        """Start streaming and processing IQ data."""
        if self.is_streaming:
            return {"error": "Already streaming"}
        if not self.client.connected:
            return {"error": "Not connected to RTL-TCP"}

        self.is_streaming = True
        self._stream_task = asyncio.create_task(self._stream_loop())
        return {"streaming": True}

    async def stop_streaming(self):
        """Stop streaming."""
        self.is_streaming = False
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        # Finish any ongoing recording
        if self.is_recording:
            await self._finish_recording()
        return {"streaming": False}

    async def start_recording(self):
        """Manually start recording."""
        if self.is_recording:
            return {"error": "Already recording"}
        self.is_recording = True
        self._recording_buffer = []
        self._recording_start_time = time.time()
        self._silence_start = None
        return {"recording": True}

    async def stop_recording(self):
        """Manually stop recording."""
        if not self.is_recording:
            return {"error": "Not recording"}
        return await self._finish_recording()

    def get_new_recordings(self) -> list[dict]:
        """Get and clear new recordings list."""
        recordings = self._new_recordings.copy()
        self._new_recordings.clear()
        return recordings

    def on_new_recording(self, callback):
        """Register callback for new recordings."""
        self._callbacks.append(callback)

    def on_spectrum(self, callback):
        """Register callback for spectrum data."""
        self._spectrum_callbacks.append(callback)

    async def _stream_loop(self):
        """Main streaming loop - reads IQ data, demodulates, handles recording.

        Optimized: FFT/spectrum is computed every Nth iteration to reduce CPU
        contention with audio demodulation (which must stay realtime).
        """
        logger.info("Streaming started")
        consecutive_errors = 0
        max_errors = 50  # Allow up to 50 consecutive read errors before giving up
        _spectrum_counter = 0
        _SPECTRUM_INTERVAL = 2  # Compute FFT every 2nd iteration (~15 Hz at 32ms/iter)
        try:
            while self.is_streaming and self.client.connected:
                try:
                    # Read IQ samples — time-based chunk for consistent waterfall
                    # refresh rate regardless of sample rate.
                    # ~50ms of audio → ~10 Hz spectrum update, audio stays smooth.
                    #   1.024 MSPS → 51200 samples → 102400 bytes (~100 KB)
                    #     250 kSPS → 12500 samples →  25000 bytes (~25 KB)
                    samples_per_chunk = max(1024, int(self.client._sample_rate * 0.05))
                    iq_data = await self.client.read_samples(samples_per_chunk * 2)
                    if not iq_data:
                        # Normal: timeout or no data yet, just wait and retry
                        await asyncio.sleep(0.01)
                        continue

                    consecutive_errors = 0  # Reset on successful read

                    # Compute signal level for ONLY the tuned channel bandwidth
                    # (was: total-bandwidth power, which reacted to nearby signals)
                    self._signal_level = compute_signal_level(
                        iq_data,
                        sample_rate=self.client._sample_rate,
                        mode=self.mode,
                    )

                    # Demodulate FIRST (audio must stay realtime; FFT can wait)
                    sample_rate = self.client._sample_rate
                    audio = demodulate(iq_data, self.mode, sample_rate, self.audio_rate)

                    if len(audio) > 0:
                        self._audio_buffer.append(audio)
                        # Keep buffer manageable
                        if len(self._audio_buffer) > 100:
                            self._audio_buffer = self._audio_buffer[-50:]

                        # Live audio streaming to WebSocket subscribers
                        # (demodulation already done; broadcast is cheap fan-out)
                        if audio_broadcaster.has_listeners:
                            await audio_broadcaster.broadcast(
                                audio, self.audio_rate
                            )

                    # Compute FFT spectrum every Nth iteration to save CPU for audio
                    _spectrum_counter += 1
                    if _spectrum_counter >= _SPECTRUM_INTERVAL:
                        _spectrum_counter = 0
                        self._compute_spectrum(iq_data)

                    # Auto recording logic
                    if self._auto_record and self.is_streaming:
                        above_squelch = self._signal_level > self._squelch_level

                        if above_squelch and not self.is_recording:
                            # Signal detected, start recording
                            await self._auto_start_recording()
                        elif above_squelch and self.is_recording:
                            # Still receiving signal
                            self._silence_start = None
                            if len(audio) > 0:
                                self._recording_buffer.append(audio)
                        elif not above_squelch and self.is_recording:
                            # Signal dropped
                            if self._silence_start is None:
                                self._silence_start = time.time()
                                if len(audio) > 0:
                                    self._recording_buffer.append(audio)
                            elif time.time() - self._silence_start > settings.VAD_SILENCE_TIMEOUT:
                                # Silence timeout, stop recording
                                await self._finish_recording()

                        # Max recording duration check
                        if self.is_recording and self._recording_start_time:
                            elapsed = time.time() - self._recording_start_time
                            if elapsed > settings.MAX_RECORDING_DURATION:
                                await self._finish_recording()

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
        """Compute FFT spectrum from IQ data and notify callbacks.

        Optimized: sends base64-encoded int16 binary instead of JSON list,
        which is ~10x faster than `.tolist()` + JSON serialization.
        """
        try:
            raw = np.frombuffer(iq_data, dtype=np.uint8)
            if len(raw) < 1024:  # Need more data for 1024 bins
                return

            iq = (raw.astype(np.float32) - 127.5) / 127.5
            complex_data = iq[0::2] + 1j * iq[1::2]

            if len(complex_data) < 512:
                return

            # Larger FFT size for higher resolution
            fft_size = min(4096, len(complex_data))

            # Apply Blackman window to reduce spectral leakage
            window = np.blackman(fft_size)
            windowed = complex_data[:fft_size] * window

            # FFT
            fft_result = np.fft.fftshift(np.abs(np.fft.fft(windowed)) / fft_size)

            # Convert to dB
            fft_db = 20 * np.log10(fft_result + 1e-10)

            # Exponential moving average for smoother waterfall
            if self._spectrum_prev is not None and len(self._spectrum_prev) == len(fft_db):
                fft_db = self._spectrum_alpha * fft_db + (1 - self._spectrum_alpha) * self._spectrum_prev
            self._spectrum_prev = fft_db.copy()

            # Downsample to configured number of bins for websocket
            target_bins = self._fft_bins
            num_bins = min(target_bins, len(fft_db))
            indices = np.linspace(0, len(fft_db) - 1, num_bins, dtype=int)
            spectrum = fft_db[indices]

            # Pad to target_bins if needed
            if len(spectrum) < target_bins:
                spectrum = np.pad(spectrum, (0, target_bins - len(spectrum)), mode='edge')

            # Encode as base64 int16 binary (much faster than .tolist() JSON)
            # Client decodes: atob → charCodeAt pairs → int16
            import base64
            spectrum_int16 = np.clip(spectrum, -32768, 32767).astype(np.int16)
            spectrum_b64 = base64.b64encode(spectrum_int16.tobytes()).decode('ascii')

            self._last_spectrum = spectrum_b64

            # Notify callbacks — send base64 string (client decodes)
            for cb in self._spectrum_callbacks:
                try:
                    cb(spectrum_b64)
                except Exception as e:
                    logger.error(f"Spectrum callback error: {e}")
        except Exception as e:
            logger.error(f"FFT error: {e}")

    async def _auto_start_recording(self):
        """Auto-start recording when signal detected."""
        self.is_recording = True
        self._recording_buffer = []
        self._recording_start_time = time.time()
        self._silence_start = None
        logger.info(f"🎙️ Recording started (signal: {self._signal_level:.1f} dB)")

    async def _finish_recording(self) -> dict:
        """Finish recording and save to file."""
        if not self._recording_buffer:
            self.is_recording = False
            return {"error": "No audio data"}

        # Concatenate audio
        audio_data = np.concatenate(self._recording_buffer)

        # Check minimum duration
        duration = len(audio_data) / self.audio_rate
        if duration < settings.VAD_SILENCE_TIMEOUT:
            self.is_recording = False
            self._recording_buffer = []
            return {"error": "Recording too short"}

        # Save to WAV file
        recordings_dir = Path(settings.RECORDINGS_DIR)
        recordings_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{self.client._frequency}_{self.mode.value}.wav"
        filepath = recordings_dir / filename

        # Save as WAV
        sf.write(str(filepath), audio_data, self.audio_rate, subtype="PCM_16")

        recording_info = {
            "id": str(uuid.uuid4()),
            "filename": filename,
            "filepath": str(filepath),
            "url": f"/recordings/{filename}",
            "duration": round(duration, 2),
            "frequency": self.client._frequency,
            "mode": self.mode.value,
            "signal_level": round(self._signal_level, 1),
            "timestamp": datetime.now().isoformat(),
            "transcription": None,
        }

        self._last_recording_path = str(filepath)
        self._new_recordings.append(recording_info)

        logger.info(
            f"✅ Recording saved: {filename} ({duration:.1f}s, "
            f"{self.mode.value}, {self.client._frequency/1e6:.3f} MHz)"
        )

        # Notify callbacks
        for cb in self._callbacks:
            try:
                await cb(recording_info)
            except Exception as e:
                logger.error(f"Callback error: {e}")

        # Reset
        self.is_recording = False
        self._recording_buffer = []
        self._recording_start_time = None
        self._silence_start = None

        return recording_info


# Global SDR manager instance
sdr_manager = SDRManager()