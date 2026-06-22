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
from backend.sdr.connection_pool import (
    pool as connection_pool, PRIORITY_LIVE, PRIORITY_IDLE,
)
from backend.audio.demod import (
    DemodMode, demodulate, compute_signal_level, StreamingDemodulator,
)
from backend.audio.broadcaster import audio_broadcaster
from backend.database import async_session
from backend.models.recording import Recording, RecordingStatus
from backend.models.telegram_alert import TelegramAlert
from backend.services.app_settings import get_setting, set_setting
from backend.services.stt import transcribe, _get_stt_config
from sqlalchemy import select, func, delete

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
    # Recording settings (global — shared by all users)
    "squelch_level": settings.SQUELCH_LEVEL,
    "vad_silence_timeout": settings.VAD_SILENCE_TIMEOUT,
    "max_recording_duration": settings.MAX_RECORDING_DURATION,
    "min_recording_duration": 2,
    "auto_record": True,
    # Retention policy (0 = unlimited)
    "retention_max_count": 0,
    "retention_max_total_duration_sec": 0,
    "retention_max_age_days": 0,
    "retention_action": "keep_transcript",  # keep_transcript | delete_record
}


class SDRManager:
    """Manages SDR operations: connection, streaming, demodulation, recording.

    Recording is dual-track: auto and manual recordings are fully independent.
    Both can run simultaneously with separate buffers.
    """

    # Channel bandwidth (Hz) — user-adjustable via /api/sdr/bandwidth.
    # Default 12kHz = NBFM voice. Wider values for WBFM/broadcast.
    _DEFAULT_CHANNEL_BW = 12000
    # Idle streaming timeout: when no listeners, count down to release the
    # device so background recording jobs can use it.
    _IDLE_STREAMING_TIMEOUT = 60  # seconds

    def __init__(self):
        persisted = self._load_persisted_settings()
        # Keep a plain client for status/host/port before the first connect.
        # On connect() we swap self.client to a SharedConnectionConsumer from
        # the connection pool so that recording jobs and live listening share
        # one RTL-TCP connection per device.
        self._legacy_client = RtlTcpClient(host=persisted["host"], port=persisted["port"])
        self._legacy_client._frequency = persisted["frequency"]
        self._legacy_client._sample_rate = persisted["sample_rate"]
        self._legacy_client._gain = persisted["gain"]
        self._legacy_client._ppm = persisted["freq_correction_ppm"]
        self.client = self._legacy_client
        self._consumer_id: Optional[str] = None
        self._pool_device_id: Optional[str] = None
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
        self._auto_record = persisted["auto_record"]
        # Idle streaming timeout: when no listeners, count down to release
        # the device so background recording jobs can use it.
        self._idle_since: Optional[float] = None

        self._audio_buffer: list[np.ndarray] = []
        self._signal_level = -120.0
        self._squelch_level = persisted["squelch_level"]
        self._vad_silence_timeout = persisted["vad_silence_timeout"]
        self._max_recording_duration = persisted["max_recording_duration"]
        self._min_recording_duration = persisted["min_recording_duration"]
        self._retention_max_count = persisted["retention_max_count"]
        self._retention_max_total_duration_sec = persisted["retention_max_total_duration_sec"]
        self._retention_max_age_days = persisted["retention_max_age_days"]
        self._retention_action = persisted["retention_action"]
        self._last_recording_path: Optional[str] = None
        self._new_recordings: list[dict] = []
        self._callbacks: list = []
        self._spectrum_callbacks: list = []
        self._last_spectrum: list = []
        self._spectrum_prev: Optional[np.ndarray] = None  # For exponential averaging
        self._spectrum_alpha: float = 0.2  # EMA factor (lower = smoother, less noise)
        self._fft_bins: int = 1024  # FFT output bins (configurable: 256-8192, power of 2)
        self._channel_bw: int = self._DEFAULT_CHANNEL_BW
        # VFO offset (Hz) relative to center frequency. 0 = decode at center.
        # Non-zero → demodulate at (center + offset) without retuning the SDR
        # (OpenWebRX-style offset tuning). Users can move the listening cursor
        # within the passband without admin rights to retune the hardware.
        self._vfo_offset_hz: float = 0.0
        # VFO passband margin ratio (configurable via app settings 0.40–0.49).
        # Controls how far from center the VFO can tune (max_offset = sample_rate * ratio).
        self._vfo_margin_ratio: float = 0.40
        self._active_device_id: Optional[str] = None
        # Per-device waterfall dB range (cached from active device config).
        # When None, the frontend falls back to its global/localStorage values.
        self._device_waterfall_db_min: Optional[int] = None
        self._device_waterfall_db_max: Optional[int] = None
        self._stream_task: Optional[asyncio.Task] = None
        # Stateful streaming demodulator — carries filter state across chunks
        # to eliminate per-chunk boundary clicks (~20 Hz buzz) in live audio
        # and recordings. Reset whenever mode/sample_rate/audio_rate/channel_bw
        # (NBFM↔WBFM) changes.
        # StreamingDemodulator self-detects mode/sample_rate/audio_rate/NBFM↔WBFM
        # changes and resets its filter state internally, so no extra tracking
        # fields are needed here.
        self._demod = StreamingDemodulator()
        # Track the listener count so we can demote/promote our LIVE pool
        # consumer's priority. When there are NO listeners we demote to
        # PRIORITY_IDLE (1) so background recording jobs (PRIORITY_JOB=5) can
        # take over the tuner. When a listener connects we promote back to
        # PRIORITY_LIVE (10) so the user retunes instantly.
        self._last_listener_count: int = 0
        audio_broadcaster.on_listener_count_change(
            self._on_listener_count_changed
        )

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
    def vfo_frequency(self) -> int:
        """Absolute frequency currently being decoded (center + VFO offset)."""
        return int(round(self.client._frequency + self._vfo_offset_hz))

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
            "vfo_offset_hz": round(self._vfo_offset_hz, 1),
            "vfo_frequency": self.vfo_frequency,
            "vfo_margin_ratio": round(self._vfo_margin_ratio, 2),
            "listeners": audio_broadcaster.listener_count,
            "max_listeners": audio_broadcaster.max_listeners,
            "device_id": self._active_device_id,
            # Per-device waterfall dB range (None = use frontend global/localStorage).
            "waterfall_db_min": self._device_waterfall_db_min,
            "waterfall_db_max": self._device_waterfall_db_max,
            # Background recording jobs active on any device (for dashboard
            # indicator below the clock).
            "active_job_captures": self._collect_job_captures(),
        }

    def _collect_job_captures(self) -> list:
        """Return runtime_status of all active background job captures."""
        try:
            from backend.sdr.job_scheduler import job_scheduler
            caps = []
            for cap in job_scheduler._captures.values():
                if cap.is_running:
                    caps.append(cap.runtime_status())
            return caps
        except Exception:
            return []

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
                # Recording settings (global)
                if isinstance(saved.get("squelch_level"), (int, float, str)):
                    data["squelch_level"] = float(saved["squelch_level"])
                if isinstance(saved.get("vad_silence_timeout"), (int, str)):
                    data["vad_silence_timeout"] = int(saved["vad_silence_timeout"])
                if isinstance(saved.get("max_recording_duration"), (int, str)):
                    data["max_recording_duration"] = int(saved["max_recording_duration"])
                if isinstance(saved.get("min_recording_duration"), (int, str)):
                    data["min_recording_duration"] = int(saved["min_recording_duration"])
                if isinstance(saved.get("auto_record"), bool):
                    data["auto_record"] = bool(saved["auto_record"])
                # Retention policy
                if isinstance(saved.get("retention_max_count"), (int, str)):
                    data["retention_max_count"] = int(saved["retention_max_count"])
                if isinstance(saved.get("retention_max_total_duration_sec"), (int, str)):
                    data["retention_max_total_duration_sec"] = int(saved["retention_max_total_duration_sec"])
                if isinstance(saved.get("retention_max_age_days"), (int, str)):
                    data["retention_max_age_days"] = int(saved["retention_max_age_days"])
                if isinstance(saved.get("retention_action"), str) and saved["retention_action"] in ("keep_transcript", "delete_record"):
                    data["retention_action"] = saved["retention_action"]
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
                # Recording settings (global)
                "squelch_level": self._squelch_level,
                "vad_silence_timeout": self._vad_silence_timeout,
                "max_recording_duration": self._max_recording_duration,
                "min_recording_duration": self._min_recording_duration,
                "auto_record": self._auto_record,
                "retention_max_count": self._retention_max_count,
                "retention_max_total_duration_sec": self._retention_max_total_duration_sec,
                "retention_max_age_days": self._retention_max_age_days,
                "retention_action": self._retention_action,
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
    # Device registry (active device management)
    # ------------------------------------------------------------------
    async def get_active_device(self):
        """Return the currently active SdrDevice row, or None."""
        from backend.models.sdr_device import SdrDevice
        async with async_session() as session:
            result = await session.execute(
                select(SdrDevice).where(SdrDevice.is_active == True)  # noqa: E712
            )
            return result.scalar_one_or_none()

    def _apply_device_config(self, device) -> None:
        """Apply a device's connection config (host/port only) to the client.

        Per the new recording-jobs design, device config stores ONLY host/port
        (+ optional note). Tuner parameters (frequency/sample_rate/gain/ppm)
        are intentionally NOT part of the device config anymore — they are
        managed per recording-job window and via the legacy live tuner
        endpoints. We keep the current client tuner values untouched here so
        switching active devices does not clobber the live frequency with a
        stale/dummy value (which caused "Failed to connect to RTL-TCP").

        Sync; only updates the in-memory client target. The caller is
        responsible for (re)connecting if desired.
        """
        cfg = device.get_config()
        host = str(cfg.get("host") or settings.RTL_TCP_HOST).strip()
        port = int(cfg.get("port") or settings.RTL_TCP_PORT)
        self.client.set_connection(host, port)
        # NOTE: We deliberately do NOT overwrite frequency/sample_rate/gain/ppm
        # from the device config. Those values are now per-recording-job.
        # We do, however, accept them if present (backward compat with rows
        # created before the schema change) — but never with a 0/falsey value.
        if cfg.get("frequency"):
            self.client._frequency = int(cfg["frequency"])
        if cfg.get("sample_rate"):
            self.client._sample_rate = int(cfg["sample_rate"])
        if cfg.get("gain") is not None and cfg.get("gain") != "":
            try:
                self.client._gain = int(cfg["gain"])
            except (TypeError, ValueError):
                pass
        if cfg.get("freq_correction_ppm") is not None and cfg.get("freq_correction_ppm") != "":
            try:
                self.client._ppm = int(cfg["freq_correction_ppm"])
            except (TypeError, ValueError):
                pass
        # Cache per-device waterfall dB range (frontend applies device-specific
        # color scale on "ฟังสด"). None/missing → frontend uses global/localStorage.
        try:
            _wmin = cfg.get("waterfall_db_min")
            self._device_waterfall_db_min = int(_wmin) if _wmin is not None else None
        except (TypeError, ValueError):
            self._device_waterfall_db_min = None
        try:
            _wmax = cfg.get("waterfall_db_max")
            self._device_waterfall_db_max = int(_wmax) if _wmax is not None else None
        except (TypeError, ValueError):
            self._device_waterfall_db_max = None
        # Keep legacy sdr_settings.json in sync (host/port at minimum)
        self._save_persisted_settings()
        logger.info(f"Applied device '{device.name}' ({device.type}) config: "
                    f"{host}:{port} (tuner values preserved: "
                    f"{self.client._frequency} Hz, {self.client._sample_rate} SPS, "
                    f"gain {self.client._gain})")

    async def load_active_device(self) -> bool:
        """Load and apply the active device's config at startup.

        Called during lifespan startup. If no active device exists (e.g. the
        table is empty), falls back to the current client values (already
        loaded from sdr_settings.json by __init__).
        Returns True if an active device was found and applied.
        """
        try:
            device = await self.get_active_device()
            if device is None:
                logger.info("No active SDR device in DB; using legacy config")
                return False
            self._active_device_id = device.id
            self._apply_device_config(device)
            return True
        except Exception as e:
            logger.warning(f"Failed to load active device, using legacy config: {e}")
            return False

    async def activate_device(self, device_id: str, reconnect: bool = True) -> dict:
        """Set a device as active and apply its config.

        - Marks the given device as the only active one in the DB.
        - Applies its config (host/port/freq/sr/gain/ppm) to the client.
        - If `reconnect` and currently connected/streaming, reconnect so the
          new device takes effect immediately.

        Returns {"success": True, "device": <dict>, "status": <status>}.
        Raises ValueError if the device id is not found.
        """
        from backend.models.sdr_device import SdrDevice
        async with async_session() as session:
            device = await session.get(SdrDevice, device_id)
            if device is None:
                raise ValueError(f"Device not found: {device_id}")

            # Mark only this device as active
            result = await session.execute(select(SdrDevice))
            for d in result.scalars().all():
                d.is_active = (d.id == device_id)
            await session.commit()
            device_dict = device.to_dict()

        # Cache the active device id for status reporting
        self._active_device_id = device_id
        # Apply the config to the client (in-memory)
        self._apply_device_config(device)

        # Reconnect if we were connected, so the new device takes effect now
        was_streaming = self.is_streaming
        if reconnect and (self.client.connected or was_streaming):
            try:
                await self.disconnect()
                await asyncio.sleep(0.3)
                connect_result = await self.connect()
                if was_streaming and connect_result.get("success"):
                    await self.start_streaming()
            except Exception as e:
                logger.error(f"Reconnect after activating device failed: {e}")

        logger.info(f"Activated SDR device '{device.name}' ({device_id})")
        return {"success": True, "device": device_dict, "status": self.status}

    async def sync_active_device_config(self) -> None:
        """Persist current client tuner values back to the active device.

        Called whenever host/port/freq/gain/ppm change via the legacy endpoints
        so the active device row stays in sync with the live client state.
        Silently no-ops if there is no active device.
        """
        try:
            from backend.models.sdr_device import SdrDevice
            async with async_session() as session:
                result = await session.execute(
                    select(SdrDevice).where(SdrDevice.is_active == True)  # noqa: E712
                )
                device = result.scalar_one_or_none()
                if device is None:
                    return
                cfg = device.get_config()
                cfg["host"] = self.client.host
                cfg["port"] = self.client.port
                cfg["frequency"] = self.client._frequency
                cfg["sample_rate"] = self.client._sample_rate
                cfg["gain"] = self.client._gain
                cfg["freq_correction_ppm"] = self.client._ppm
                device.set_config(cfg)
                await session.commit()
        except Exception as e:
            logger.warning(f"Failed to sync active device config: {e}")

    # ------------------------------------------------------------------
    # Connection lifecycle (via shared connection pool)
    # ------------------------------------------------------------------
    async def connect(self, stop_if_streaming: bool = True) -> dict:
        """Connect to RTL-TCP server via the shared connection pool.

        Acquires a SharedConnectionConsumer with PRIORITY_LIVE so that
        recording jobs on the same device yield to interactive listening.

        When ``stop_if_streaming`` is False the currently running stream loop
        is left intact — used by in-loop reconnection so a transient RTL-TCP
        drop does not tear down streaming (which previously made the
        "รับสัญญาณ" button flip back and forth shortly after pressing it).
        """
        if stop_if_streaming and self.is_streaming:
            await self.stop_streaming()

        # Release any existing pool consumer first
        await self._release_pool_consumer()

        # Determine device_id and host/port from current client state
        device_id = self._active_device_id or "_legacy"
        host = self.client.host
        port = self.client.port

        # Acquire a shared consumer for live listening. Use PRIORITY_IDLE when
        # there are no listeners so background recording jobs (PRIORITY_JOB=5)
        # can take over the tuner immediately. The listener-count callback
        # (_on_listener_count_changed) will promote us back to PRIORITY_LIVE as
        # soon as a user connects to /ws/audio. This is what allows scheduled
        # jobs to run even while the 24/7 autostart stream is up.
        initial_prio = (
            PRIORITY_LIVE if audio_broadcaster.listener_count > 0
            else PRIORITY_IDLE
        )
        consumer = await connection_pool.acquire(
            device_id, host, port,
            consumer_id="live", priority=initial_prio,
        )
        if initial_prio == PRIORITY_IDLE:
            logger.info(
                "🎛️ LIVE consumer acquired with PRIORITY_IDLE "
                "(no listeners — jobs may take the tuner)"
            )
        # Preconfigure tuner values before connecting so the RTL-TCP
        # handshake sends correct freq/sr/gain/ppm.
        #
        # CRITICAL: if a background JOB is already the controller on the
        # shared connection, we must adopt ITS tuner values — NOT our own
        # persisted ones. Otherwise the RTL-TCP handshake (client.connect())
        # sends set_frequency/set_sample_rate straight to the dongle,
        # bypassing the priority arbitration, and silently overwrites the
        # JOB's tuner settings. Symptom: at boot the autostart LIVE consumer
        # connects after the JOB has already reclaimed the tuner, the
        # handshake re-tunes to the stale persisted center (e.g. 168.024 MHz
        # vs the JOB's 168.512 MHz), and the JOB then demodulates at the
        # wrong center → "เสียงซ่าๆ".
        shared_client = getattr(consumer._conn, "client", None)
        has_controller = (
            shared_client is not None
            and getattr(consumer._conn, "_controller_id", None) is not None
        )
        if has_controller and shared_client.connected:
            # Adopt the controller's actual tuner state so we do NOT disturb it.
            pre_freq = shared_client._frequency
            pre_sr = shared_client._sample_rate
            pre_gain = shared_client._gain
            pre_ppm = shared_client._ppm
            logger.info(
                f"🎛️ LIVE connect adopting controller tuner "
                f"(center={pre_freq/1e6:.4f} MHz sr={pre_sr}) — "
                f"will NOT overwrite the active JOB"
            )
        else:
            pre_freq = self.client._frequency
            pre_sr = self.client._sample_rate
            pre_gain = self.client._gain
            pre_ppm = self.client._ppm
        consumer.preconfigure(pre_freq, pre_sr, pre_gain, pre_ppm)
        ok = await consumer.connect()
        if ok:
            self.client = consumer
            self._consumer_id = "live"
            self._pool_device_id = device_id
        else:
            # Clean up: release the failed consumer so the pool can tear down
            # the failed connection (otherwise refcount leak blocks reconnect).
            try:
                await connection_pool.release(device_id, "live")
            except Exception:
                pass
            # Revert to legacy client for status reporting
            self.client = self._legacy_client
            self._consumer_id = None
            self._pool_device_id = None
        self._save_persisted_settings()
        return {"success": ok, "status": self.status}

    async def _release_pool_consumer(self) -> None:
        """Release the current pool consumer and revert to legacy client."""
        if self._consumer_id and self._pool_device_id:
            # Sync tuner values back to legacy client before releasing
            if hasattr(self.client, '_frequency'):
                self._legacy_client._frequency = self.client._frequency
                self._legacy_client._sample_rate = self.client._sample_rate
                self._legacy_client._gain = self.client._gain
                self._legacy_client._ppm = self.client._ppm
                self._legacy_client.host = self.client.host
                self._legacy_client.port = self.client.port
            await connection_pool.release(
                self._pool_device_id, self._consumer_id
            )
            self._consumer_id = None
            self._pool_device_id = None
            self.client = self._legacy_client

    async def disconnect(self):
        """Disconnect from RTL-TCP (release pool consumer)."""
        await self.stop_streaming()
        await self._release_pool_consumer()

    # FM broadcast band (WBFM) — auto-select wide channel bandwidth when tuning here.
    WBFM_BANDS = [(65_800_000, 74_000_000), (76_000_000, 95_000_000), (87_500_000, 108_000_000)]
    WBFM_CHANNEL_BW = 200_000

    def _is_wbfm_band(self, freq_hz: int) -> bool:
        return any(lo <= freq_hz <= hi for lo, hi in self.WBFM_BANDS)

    async def set_frequency(self, freq_hz: int):
        """Set center frequency (auto-adjusts BW for FM broadcast band).

        When center frequency changes, reset the VFO offset to 0 (the old
        offset was relative to the old center and would point elsewhere).
        """
        await self.client.set_frequency(freq_hz)
        await self._on_shared_tuner_changed()
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
        await self.sync_active_device_config()

    async def set_sample_rate(self, rate: int):
        await self.client.set_sample_rate(rate)
        self._save_persisted_settings()
        await self.sync_active_device_config()

    async def set_gain(self, gain_db: int):
        await self.client.set_gain(gain_db)
        self._save_persisted_settings()
        await self.sync_active_device_config()

    async def set_freq_correction(self, ppm: int):
        await self.client.set_freq_correction(ppm)
        self._save_persisted_settings()
        await self.sync_active_device_config()

    def _clamp_vfo_offset(self) -> bool:
        """Clamp our VFO offset into the current passband (defensive guard).

        Returns True if the offset was changed (clamped or reset). Called
        whenever the shared tuner's center/sample_rate may have changed
        out from under us. If the offset still fits in the new passband
        we keep it (preserve the user's listening position); otherwise we
        reset to 0 (center).
        """
        sr = self.client._sample_rate or 0
        if sr <= 0:
            return False
        max_offset = sr * self._vfo_margin_ratio
        off = self._vfo_offset_hz
        if abs(off) > max_offset + 0.5:
            # No longer fits → reset to center.
            self._vfo_offset_hz = 0.0
            self._vfo_phase = 0.0
            self._demod.set_vfo_offset(0.0)
            return True
        return False

    async def _on_shared_tuner_changed(self) -> None:
        """React to a center frequency / sample rate change on the shared tuner.

        Called from two places:
          1. Our own set_frequency() (LIVE path).
          2. DeviceCapture._reclaim_tuner() via notify_live_tuner_changed()
             (JOB path) — when a background recording job retunes the SHARED
             device, our consumer mirror goes stale and our VFO offset
             (relative to the OLD center) now points elsewhere. We must sync
             the mirror from the shared client, reset / clamp our own demod
             offset, and reset every listener's offset, otherwise the
             dashboard shows a "listening" frequency OUTSIDE the new passband
             (e.g. 168.512 MHz center + a stale +751 kHz offset = 169.263 MHz
             → off-screen cursor + pure-noise audio).
        """
        shared = getattr(self.client, "_conn", None)
        shared_client = getattr(shared, "client", None) if shared is not None else None
        if shared_client is not None:
            # Sync our consumer-side mirror to the ACTUAL shared tuner state,
            # because the retune may have been issued by a different consumer
            # (the JOB) and our mirror (self.client._frequency) is now stale.
            self.client._frequency = shared_client._frequency
            self.client._sample_rate = shared_client._sample_rate
            self.client._gain = shared_client._gain
            self.client._ppm = shared_client._ppm
        # Defensive clamp: if the existing offset still fits in the new
        # passband, keep it; otherwise reset to 0.
        self._clamp_vfo_offset()
        # Reset EVERY listener's per-user VFO offset, since their offsets
        # were relative to the old center frequency too.
        audio_broadcaster.reset_all_vfo()
        # Persist the (now-current) shared tuner state so a restart does not
        # boot back to a stale center/sample_rate that the JOB had moved away
        # from.
        self._save_persisted_settings()

    def set_channel_bw(self, bw_hz: int):
        if not (1000 <= bw_hz <= 500000):
            raise ValueError(f"Invalid channel BW: {bw_hz}. Must be 1kHz–500kHz")
        self._channel_bw = bw_hz
        logger.info(f"Channel BW set to {bw_hz} Hz ({bw_hz/1e3:.0f} kHz)")

    def set_vfo_offset(self, offset_hz: float) -> dict:
        """Set the VFO offset (Hz) relative to center frequency.

        This lets non-admin users move the listening cursor within the SDR
        passband without retuning the hardware. The offset must stay within
        ±(margin_ratio) * sample_rate (default 0.4) to remain inside the
        visible passband (otherwise the signal would alias / be outside the
        waterfall). The margin_ratio is configurable via app settings
        ("sdr_vfo_margin_ratio", 0.40–0.49).
        """
        offset_hz = float(offset_hz)
        max_offset = self.client._sample_rate * self._vfo_margin_ratio
        if abs(offset_hz) > max_offset:
            raise ValueError(
                f"VFO offset {offset_hz:.0f} Hz outside passband "
                f"(±{max_offset:.0f} Hz)"
            )
        self._vfo_offset_hz = offset_hz
        self._demod.set_vfo_offset(offset_hz)
        logger.info(
            f"🎯 VFO offset set to {offset_hz:.0f} Hz "
            f"(listening at {(self.client._frequency + offset_hz)/1e6:.4f} MHz)"
        )
        return {"vfo_offset_hz": self._vfo_offset_hz, "vfo_frequency": self.vfo_frequency}

    def set_vfo_frequency(self, freq_hz: int) -> dict:
        """Set VFO by absolute frequency. Computes offset from center freq."""
        offset = float(freq_hz) - float(self.client._frequency)
        return self.set_vfo_offset(offset)

    async def load_vfo_margin_ratio(self) -> None:
        """Load VFO passband margin ratio from app settings (startup).

        Stored under key "sdr_vfo_margin_ratio" as a float string (0.40–0.49).
        Defaults to 0.40 if unset.
        """
        try:
            raw = await get_setting("sdr_vfo_margin_ratio", "0.40")
            self._vfo_margin_ratio = self._validate_margin_ratio(float(raw))
            logger.info(f"VFO margin ratio loaded: {self._vfo_margin_ratio:.2f}")
        except Exception as e:
            logger.warning(f"Failed to load VFO margin ratio, using 0.40: {e}")
            self._vfo_margin_ratio = 0.40
        # Keep the per-listener broadcaster in sync so listener VFO clamping
        # uses the same ratio.
        audio_broadcaster.vfo_margin_ratio = self._vfo_margin_ratio

    async def set_vfo_margin_ratio(self, ratio: float) -> dict:
        """Set VFO passband margin ratio (0.40–0.49) and persist to app settings."""
        ratio = self._validate_margin_ratio(ratio)
        self._vfo_margin_ratio = ratio
        await set_setting("sdr_vfo_margin_ratio", f"{ratio:.2f}")
        # If current VFO offset exceeds the new limit, clamp it to the edge
        max_offset = self.client._sample_rate * self._vfo_margin_ratio
        if abs(self._vfo_offset_hz) > max_offset:
            clamped = max(-max_offset, min(max_offset, self._vfo_offset_hz))
            self._vfo_offset_hz = clamped
            self._demod.set_vfo_offset(clamped)
        # Propagate to the per-listener broadcaster (used for clamping each
        # listener's VFO offset).
        audio_broadcaster.vfo_margin_ratio = self._vfo_margin_ratio
        logger.info(f"VFO margin ratio set to {ratio:.2f} (max offset {max_offset:.0f} Hz)")
        return {"vfo_margin_ratio": self._vfo_margin_ratio}

    @staticmethod
    def _validate_margin_ratio(ratio: float) -> float:
        """Validate margin ratio is in [0.40, 0.49]."""
        ratio = float(ratio)
        if not (0.40 <= ratio <= 0.49):
            raise ValueError(f"VFO margin ratio must be 0.40–0.49, got {ratio}")
        return round(ratio, 2)

    # ------------------------------------------------------------------
    # Max listeners (configurable concurrent listener cap)
    # ------------------------------------------------------------------
    _MIN_MAX_LISTENERS = 1
    _HARD_CAP_LISTENERS = 128

    @classmethod
    def _validate_max_listeners(cls, n: int) -> int:
        """Clamp max listeners to [_MIN_MAX_LISTENERS, _HARD_CAP_LISTENERS]."""
        n = int(n)
        if n < cls._MIN_MAX_LISTENERS:
            n = cls._MIN_MAX_LISTENERS
        elif n > cls._HARD_CAP_LISTENERS:
            n = cls._HARD_CAP_LISTENERS
        return n

    async def load_max_listeners(self) -> None:
        """Load max listeners from app settings (startup).

        Stored under key "sdr_max_listeners" as an integer string (1–128).
        Defaults to 8 if unset.
        """
        try:
            raw = await get_setting("sdr_max_listeners", "8")
            n = self._validate_max_listeners(int(raw))
            audio_broadcaster.set_max_listeners(n)
            logger.info(f"Max listeners loaded: {n}")
        except Exception as e:
            logger.warning(f"Failed to load max listeners, using 8: {e}")
            audio_broadcaster.set_max_listeners(8)

    async def set_max_listeners(self, n: int) -> dict:
        """Set max listeners (1–128) and persist to app settings."""
        n = self._validate_max_listeners(n)
        actual = audio_broadcaster.set_max_listeners(n)
        await set_setting("sdr_max_listeners", str(actual))
        logger.info(
            f"Max listeners set to {actual} "
            f"(current: {audio_broadcaster.listener_count})"
        )
        return {
            "max_listeners": actual,
            "current_listeners": audio_broadcaster.listener_count,
        }

    async def set_mode(self, mode: str):
        self.mode = DemodMode(mode.upper())

    def set_squelch(self, level_db: float):
        self._squelch_level = level_db
        self._save_persisted_settings()

    def set_recording_config(
        self,
        auto_record: Optional[bool] = None,
        squelch_level: Optional[float] = None,
        vad_silence_timeout: Optional[int] = None,
        max_recording_duration: Optional[int] = None,
        min_recording_duration: Optional[int] = None,
        retention_max_count: Optional[int] = None,
        retention_max_total_duration_sec: Optional[int] = None,
        retention_max_age_days: Optional[int] = None,
        retention_action: Optional[str] = None,
    ) -> dict:
        """Update global recording config and persist to disk (shared by all users)."""
        if auto_record is not None:
            self._auto_record = bool(auto_record)
        if squelch_level is not None:
            self._squelch_level = float(squelch_level)
        if vad_silence_timeout is not None:
            if vad_silence_timeout < 1:
                raise ValueError("vad_silence_timeout must be >= 1 second")
            self._vad_silence_timeout = int(vad_silence_timeout)
        if max_recording_duration is not None:
            if max_recording_duration < 1:
                raise ValueError("max_recording_duration must be >= 1 second")
            self._max_recording_duration = int(max_recording_duration)
        if min_recording_duration is not None:
            if min_recording_duration < 0:
                raise ValueError("min_recording_duration must be >= 0 seconds")
            self._min_recording_duration = int(min_recording_duration)
        if retention_max_count is not None:
            if retention_max_count < 0:
                raise ValueError("retention_max_count must be >= 0")
            self._retention_max_count = int(retention_max_count)
        if retention_max_total_duration_sec is not None:
            if retention_max_total_duration_sec < 0:
                raise ValueError("retention_max_total_duration_sec must be >= 0")
            self._retention_max_total_duration_sec = int(retention_max_total_duration_sec)
        if retention_max_age_days is not None:
            if retention_max_age_days < 0:
                raise ValueError("retention_max_age_days must be >= 0")
            self._retention_max_age_days = int(retention_max_age_days)
        if retention_action is not None:
            if retention_action not in ("keep_transcript", "delete_record"):
                raise ValueError("retention_action must be 'keep_transcript' or 'delete_record'")
            self._retention_action = retention_action
        self._save_persisted_settings()
        # หลังเปลี่ยน config ให้ enforce ทันที (ผู้ใช้อาจลด limit)
        self._spawn_enforce_retention()
        return self.get_recording_config()

    def get_recording_config(self) -> dict:
        """Return current global recording config."""
        return {
            "auto_record": self._auto_record,
            "squelch_level": self._squelch_level,
            "vad_silence_timeout": self._vad_silence_timeout,
            "max_recording_duration": self._max_recording_duration,
            "min_recording_duration": self._min_recording_duration,
            "retention_max_count": self._retention_max_count,
            "retention_max_total_duration_sec": self._retention_max_total_duration_sec,
            "retention_max_age_days": self._retention_max_age_days,
            "retention_action": self._retention_action,
        }

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
        # Auto-connect if not already connected, so "รับสัญญาณ" works without
        # first pressing "ฟังสด". Previously this returned an error when nobody
        # had connected yet, which forced users to press "ฟังสด" first and also
        # caused the toggle button to flip back ("วนอยู่แบบนี้").
        if not self.client.connected:
            try:
                connect_result = await self.connect(stop_if_streaming=False)
            except Exception as e:
                return {"error": f"ไม่สามารถเชื่อมต่อ RTL-TCP ได้: {e}"}
            if not connect_result.get("success"):
                return {"error": "ไม่สามารถเชื่อมต่อ RTL-TCP ได้ กรุณาตรวจสอบอุปกรณ์/เซิร์ฟเวอร์"}
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

    def _on_listener_count_changed(self, count: int) -> None:
        """Broadcaster callback: promote/demote our LIVE pool consumer.

        Called synchronously from AudioBroadcaster.subscribe/unsubscribe
        (on the event-loop thread) whenever the listener count changes.

        - count > 0 : promote consumer "live" to PRIORITY_LIVE (10) so the
          user can retune instantly and the tuner follows the user. The
          promotion triggers preemption, which pauses any background job
          (which then demodulates at an offset or waits).
        - count == 0 : demote consumer "live" to PRIORITY_IDLE (1) so any
          background recording job (PRIORITY_JOB=5) outranks it and can
          reclaim the tuner + re-apply its own freq/gain/ppm.

        This is scheduled as a task because set_priority() is async and we
        are invoked from a sync broadcaster path.
        """
        self._last_listener_count = count
        target = PRIORITY_LIVE if count > 0 else PRIORITY_IDLE
        # Only act if we currently own a pool consumer (connected) and the
        # consumer exposes set_priority (SharedConnectionConsumer).
        if not self._consumer_id or not self._pool_device_id:
            return
        consumer = self.client
        set_prio = getattr(consumer, "set_priority", None)
        if not callable(set_prio):
            return

        async def _do():
            try:
                await set_prio(target)
                logger.info(
                    f"🎛️ LIVE consumer priority → {target} "
                    f"(listeners={count})"
                )
            except Exception as e:
                logger.warning(f"Failed to adjust LIVE priority: {e}")

        try:
            asyncio.create_task(_do())
        except RuntimeError:
            # No running loop (shouldn't happen here): best-effort no-op.
            pass

    def _device_has_active_jobs(self) -> bool:
        """Check if the active device has any running background recording job."""
        try:
            from backend.sdr.job_scheduler import job_scheduler
            if not self._active_device_id:
                return False
            for cap in job_scheduler._captures.values():
                if cap.device_id == self._active_device_id and cap.is_running:
                    return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Main streaming loop — dual-track recording
    # ------------------------------------------------------------------
    async def _stream_loop(self):
        logger.info("Streaming started")
        consecutive_errors = 0
        max_errors = 50
        # Bounded auto-reconnect so a transient RTL-TCP drop (network blip,
        # dongle reset, rtl_tcp restart) does not immediately end streaming
        # and flip the UI button back to "รับสัญญาณ". The loop reconnects up
        # to max_reconnect_attempts times, keeping self.is_streaming = True
        # during the retries so clients do not see a flicker.
        reconnect_attempts = 0
        max_reconnect_attempts = 5
        _spectrum_counter = 0
        _SPECTRUM_INTERVAL = 2
        try:
            while self.is_streaming:
                # If the RTL-TCP connection dropped, try to reconnect instead
                # of exiting immediately (the old `and self.client.connected`
                # condition exited on the first drop and caused the flip-flop).
                if not self.client.connected:
                    if reconnect_attempts >= max_reconnect_attempts:
                        logger.error(
                            f"RTL-TCP reconnect failed {reconnect_attempts}x; "
                            f"stopping stream"
                        )
                        break
                    reconnect_attempts += 1
                    logger.warning(
                        f"🔌 RTL-TCP disconnected mid-stream; reconnecting "
                        f"({reconnect_attempts}/{max_reconnect_attempts})..."
                    )
                    try:
                        rc = await self.connect(stop_if_streaming=False)
                    except Exception as e:
                        logger.error(f"Reconnect attempt raised: {e}")
                        rc = {"success": False}
                    if not rc.get("success"):
                        # Back off before the next attempt (linear up to 5s)
                        await asyncio.sleep(min(2.0 * reconnect_attempts, 5.0))
                        continue
                    # Reconnected successfully — reset counters and resume
                    reconnect_attempts = 0
                    consecutive_errors = 0
                    logger.info("🔌 RTL-TCP reconnected; resuming stream")
                    continue

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
                        vfo_offset_hz=self._vfo_offset_hz,
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
                        # OpenWebRX-style per-listener demodulation: feed the
                        # RAW IQ to every listener, each of which demodulates
                        # its OWN frequency (VFO offset) / mode / bandwidth
                        # independently. One user tuning no longer affects
                        # anyone else. feed_iq() is NON-BLOCKING: it only
                        # enqueues to each listener's iq_queue; per-listener
                        # background workers do the demod off the hot path so
                        # a slow listener can never stall the stream loop.
                        #
                        # Pass the already-computed global `audio` so that
                        # listeners on the SAME channel (mode/bw/vfo) reuse
                        # it instead of demodulating again (~2x CPU saving).
                        if audio_broadcaster.has_listeners:
                            audio_broadcaster.feed_iq(
                                iq_data, self.client._sample_rate, self.audio_rate,
                                global_audio=audio if len(audio) > 0 else None,
                                g_mode=self.mode,
                                g_channel_bw=self._channel_bw,
                                g_vfo_offset_hz=self._vfo_offset_hz,
                                g_signal_level=self._signal_level,
                            )

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
                            if elapsed > self._max_recording_duration:
                                logger.info(f"Manual recording hit max duration ({self._max_recording_duration}s), finishing")
                                await self._finish_manual_recording()

                    # NOTE: The previous "idle streaming timeout" block was
                    # removed. It stopped live streaming after 60s with no
                    # listeners whenever the active device had background
                    # recording jobs — but this harmed rather than helped:
                    #   1. It did NOT free the RTL-TCP connection (only
                    #      disconnect()/_release_pool_consumer() does), so
                    #      jobs gained nothing.
                    #   2. The connection pool already shares ONE RTL-TCP
                    #      connection via IQ fan-out between live (SDRManager)
                    #      and jobs (DeviceCapture), with priority-based
                    #      pause/resume, so live + jobs coexist fine.
                    #   3. Stopping the stream flipped the UI back to
                    #      "รับสัญญาณ" ("📡 Streaming was stopped by the
                    #      backend.") and blocked manual recording, which
                    #      requires an active stream — forcing users to press
                    #      "ฟังสด" first as a workaround.
                    # Now live streaming stays up until the user stops it or
                    # the RTL-TCP connection is genuinely lost.

                    # === TRACK 2: Auto recording (gated by active listeners) ===
                    # Auto-record from live listening requires active listeners,
                    # UNLESS the device has no background jobs (in which case we
                    # allow auto-record to continue even without listeners).
                    device_has_jobs = self._device_has_active_jobs()
                    should_auto_record = (
                        self._auto_record
                        and self.is_streaming
                        and (audio_broadcaster.has_listeners or not device_has_jobs)
                    )
                    if not should_auto_record and self._auto_recording:
                        logger.info(
                            "🎙️ Auto recording finishing: no active listeners and "
                            "device has background jobs"
                        )
                        await self._finish_auto_recording()

                    if should_auto_record:
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
                            elif time.time() - self._auto_silence_start > self._vad_silence_timeout:
                                await self._finish_auto_recording()

                        if self._auto_recording and self._auto_start_time:
                            elapsed = time.time() - self._auto_start_time
                            if elapsed > self._max_recording_duration:
                                await self._finish_auto_recording()

                    # else: auto-record paused (no listeners + device has jobs)
                    # — recording job will pick up that frequency instead.

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

            spectrum_int16 = np.clip(spectrum, -32768, 32767).astype(np.int16)
            # Send raw int16 LE bytes (no base64). The /ws endpoint wraps this
            # in a binary WebSocket frame (2-byte type header + payload), and
            # the frontend reads it directly as an Int16Array — skipping the
            # ~33% base64 expansion and the per-byte atob() loop.
            spectrum_bytes = spectrum_int16.tobytes()
            self._last_spectrum = spectrum_bytes

            for cb in self._spectrum_callbacks:
                try:
                    cb(spectrum_bytes)
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

        if duration < self._min_recording_duration:
            logger.info(f"Auto recording too short ({duration:.1f}s < min {self._min_recording_duration}s), discarding")
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

    async def _save_recording(
        self, audio_data: np.ndarray, duration: float, source: str,
        device_id: Optional[str] = None, user_id: Optional[str] = None,
    ) -> dict:
        """Save audio data to WAV file, insert into DB, and notify callbacks.

        Args:
            device_id: SDR device that produced this recording (nullable).
            user_id: For manual recordings, the user who pressed record.
        """
        recordings_dir = Path(settings.RECORDINGS_DIR)
        recordings_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # Use the VFO frequency (center + offset) as the recording's frequency
        # so the log reflects what the user was actually listening to, not the
        # SDR hardware center frequency.
        freq_hz = self.vfo_frequency
        filename = f"{timestamp}_{freq_hz}_{self.mode.value}.wav"
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
                    file_size=file_size,
                    duration=round(duration, 2),
                    transcript=None,
                    status=RecordingStatus.COMPLETED,
                    source=source,
                    is_unread=1,
                    device_id=device_id or self._active_device_id,
                    recorded_by_user_id=user_id if source == "manual" else None,
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

        # === Enforce retention policy (fire-and-forget, safe wrapper) ===
        self._spawn_enforce_retention()

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
                        rec_info_copy["transcript"] = text
                        rec_info_copy["transcription"] = text
                        rec_info_copy["stt_provider"] = provider
                        rec_info_copy["stt_model"] = model
                        rec_info_copy["stt_error"] = error
                        rec_info_copy["stt_duration"] = stt_dur
                        self._new_recordings.append({**rec_info_copy, "_transcribed": True})
                        logger.info(f"✅ Auto-transcribed {rec_id}: {len(text)} chars (provider={provider}, model={model}, duration={stt_dur}s)")
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

    # ------------------------------------------------------------------
    # Per-listener manual recording (each user records their own audio)
    # ------------------------------------------------------------------
    async def save_listener_recording(self, rec_data: dict) -> Optional[dict]:
        """Save a per-listener manual recording to disk + DB.

        `rec_data` is the dict returned by AudioBroadcaster.stop_listener_recording():
            { audio_data, duration, sample_rate, mode, vfo_offset_hz, user_id, user_role }

        The recording's frequency is THIS listener's VFO frequency (center +
        offset), so the log reflects what the user was actually listening to.

        Returns recording_info dict on success, or None if audio was empty.
        """
        audio_data = rec_data.get("audio_data")
        duration = rec_data.get("duration", 0.0)
        if audio_data is None or len(audio_data) == 0 or duration < 0.1:
            return None

        sample_rate = rec_data.get("sample_rate", self.audio_rate)
        mode_str = rec_data.get("mode", "FM")
        try:
            mode = DemodMode(mode_str)
        except ValueError:
            mode = self.mode

        recordings_dir = Path(settings.RECORDINGS_DIR)
        recordings_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # Compute this listener's actual VFO frequency
        center_freq = self.client._frequency if self.client._frequency else settings.RTL_TCP_CENTER_FREQ
        offset_hz = rec_data.get("vfo_offset_hz", 0.0)
        freq_hz = int(center_freq + offset_hz)
        filename = f"{timestamp}_{freq_hz}_{mode.value}.wav"
        filepath = recordings_dir / filename

        # Normalize float32 [-1,1] → int16 PCM and write WAV
        audio_f32 = np.asarray(audio_data, dtype=np.float32)
        sf.write(str(filepath), audio_f32, int(sample_rate), subtype="PCM_16")

        # Capture file size on disk for storage usage aggregation.
        try:
            file_size = filepath.stat().st_size
        except Exception:
            file_size = None

        rec_id = str(uuid.uuid4())
        freq_mhz = freq_hz / 1e6
        now = datetime.now(timezone.utc)
        user_id = rec_data.get("user_id")

        # Insert into DB and fetch recorder display name for WS payload
        recorder_username = None
        recorder_display_name = None
        try:
            async with async_session() as session:
                rec = Recording(
                    id=rec_id,
                    frequency=freq_hz,
                    frequency_mhz=freq_mhz,
                    mode=mode,
                    audio_file_path=str(filepath),
                    audio_file_name=filename,
                    file_size=file_size,
                    duration=round(duration, 2),
                    status=RecordingStatus.COMPLETED,
                    source="manual",
                    recorded_by_user_id=user_id,
                    is_unread=1,
                    device_id=self._active_device_id,
                    recorded_at=now,
                    created_at=now,
                )
                session.add(rec)
                # Fetch recorder's display name to embed in the WS payload so
                # the frontend can show "👤 username" immediately without a
                # follow-up fetch.
                if user_id:
                    from backend.models.user import User
                    user_obj = await session.get(User, user_id)
                    if user_obj:
                        recorder_username = user_obj.username
                        recorder_display_name = user_obj.display_name
                await session.commit()
                logger.info(f"💾 Listener recording saved to DB: {rec_id} (user={user_id})")
        except Exception as db_err:
            logger.error(f"DB insert failed for listener recording (file still saved): {db_err}")

        recording_info = {
            "id": rec_id,
            "filename": filename,
            "filepath": str(filepath),
            "url": f"/recordings/{filename}",
            "duration": round(duration, 2),
            "frequency": freq_hz,
            "frequency_mhz": freq_mhz,
            "mode": mode.value,
            "signal_level": round(self._signal_level, 1) if self._signal_level else 0,
            "signal_strength": round(self._signal_level, 1) if self._signal_level else 0,
            "timestamp": now.isoformat(),
            "recorded_at": now.isoformat(),
            "transcription": None,
            "transcript": None,
            "source": "manual",
            "status": "completed",
            "is_unread": 1,
            "recorded_by_user_id": user_id,
            "recorded_by_username": recorder_username,
            "recorded_by_display_name": recorder_display_name,
        }

        self._last_recording_path = str(filepath)
        self._new_recordings.append(recording_info)

        logger.info(
            f"✅ Listener recording saved [manual]: {filename} "
            f"({duration:.1f}s, {mode.value}, {freq_mhz:.3f} MHz, user={user_id})"
        )

        # Enforce retention policy
        self._spawn_enforce_retention()

        # Notify callbacks
        for cb in self._callbacks:
            try:
                await cb(recording_info)
            except Exception as e:
                logger.error(f"Callback error: {e}")

        # Auto-transcribe if enabled
        try:
            cfg = await _get_stt_config()
            if cfg.get("auto_transcribe") and cfg.get("api_key"):
                logger.info(f"🔄 Auto-transcribing listener recording {rec_id}...")
                rec_info_copy = dict(recording_info)

                async def _do_transcribe():
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
                        rec_info_copy["transcript"] = text
                        rec_info_copy["transcription"] = text
                        rec_info_copy["stt_provider"] = provider
                        rec_info_copy["stt_model"] = model
                        rec_info_copy["stt_error"] = error
                        rec_info_copy["stt_duration"] = stt_dur
                        self._new_recordings.append({**rec_info_copy, "_transcribed": True})
                        logger.info(f"✅ Auto-transcribed {rec_id}: {len(text)} chars (provider={provider}, model={model}, duration={stt_dur}s)")
                    except Exception as e:
                        logger.error(f"Auto-transcribe failed for {rec_id}: {e}")
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

    # ------------------------------------------------------------------
    # Retention policy enforcement
    # ------------------------------------------------------------------
    def _spawn_enforce_retention(self) -> None:
        """Spawn retention enforcement as a fire-and-forget task with error logging.

        Directly creating asyncio.create_task(self._enforce_retention()) silently
        swallows exceptions (Python logs only an "exception was never retrieved"
        warning). This wrapper guarantees that errors are logged.
        """
        async def _safe_run():
            try:
                await self._enforce_retention()
            except Exception as e:
                logger.error(f"Retention enforcement crashed: {e}", exc_info=True)

        try:
            asyncio.create_task(_safe_run())
        except RuntimeError:
            # No running event loop (e.g. called from sync context / CLI)
            logger.warning("Retention enforcement skipped: no running event loop")

    @staticmethod
    def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure a datetime is timezone-aware (assume UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    async def _enforce_retention(self) -> None:
        """Enforce retention policy: delete old audio files / records as needed.

        Counting rules:
        - delete_record: counts ALL records (kept rows <= limit)
        - keep_transcript: counts only records that still have audio
          (audio_deleted=0); transcript-only rows are archived and not counted
          so the limit applies to playable recordings.
        """
        max_count = self._retention_max_count
        max_dur = self._retention_max_total_duration_sec
        max_age = self._retention_max_age_days
        action = self._retention_action

        logger.info(
            f"Retention enforcement START | max_count={max_count}, "
            f"max_dur={max_dur}s, max_age={max_age}d, action={action}"
        )

        if max_count <= 0 and max_dur <= 0 and max_age <= 0:
            logger.info("Retention: all limits disabled (0), nothing to do")
            return
        if action not in ("keep_transcript", "delete_record"):
            logger.warning(f"Retention: invalid action '{action}', aborting")
            return

        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Recording).order_by(Recording.recorded_at.asc())
                )
                all_recs = result.scalars().all()
                logger.info(f"Retention: fetched {len(all_recs)} record(s) from DB")
                if not all_recs:
                    logger.info("Retention: no records, nothing to do")
                    return

                now = datetime.now(timezone.utc)
                to_remove: list = []
                to_remove_ids: set = set()

                # --- max_age: remove records older than cutoff ---
                if max_age > 0:
                    from datetime import timedelta
                    cutoff = now - timedelta(days=max_age)
                    for rec in all_recs:
                        rec_time = self._ensure_aware(rec.recorded_at)
                        if rec_time is not None and rec_time < cutoff:
                            if id(rec) not in to_remove_ids:
                                to_remove.append(rec)
                                to_remove_ids.add(id(rec))

                # --- max_count / max_dur: keep newest up to limit ---
                # For keep_transcript, only count records with audio (audio_deleted=0)
                kept_ids = set()
                count = 0
                total_dur = 0.0
                for rec in reversed(all_recs):  # newest first
                    if action == "keep_transcript" and getattr(rec, "audio_deleted", 0):
                        # Transcript-only archive row, don't count toward limit
                        kept_ids.add(rec.id)
                        continue
                    new_count = count + 1
                    new_dur = total_dur + (rec.duration or 0)
                    over = (max_count > 0 and new_count > max_count) or (max_dur > 0 and new_dur > max_dur)
                    if over:
                        break
                    count, total_dur = new_count, new_dur
                    kept_ids.add(rec.id)

                logger.info(
                    f"Retention: keeping {len(kept_ids)} record(s) "
                    f"(counted {count}, total_dur={total_dur:.1f}s)"
                )

                for rec in all_recs:
                    if rec.id not in kept_ids and id(rec) not in to_remove_ids:
                        to_remove.append(rec)
                        to_remove_ids.add(id(rec))

                if not to_remove:
                    logger.info("Retention: nothing to remove")
                    return

                logger.info(
                    f"Retention: removing {len(to_remove)} record(s) "
                    f"(action={action})"
                )

                for rec in to_remove:
                    # Delete audio file from disk
                    if rec.audio_file_path:
                        try:
                            fpath = Path(rec.audio_file_path)
                            if fpath.exists():
                                fpath.unlink()
                        except Exception as e:
                            logger.warning(f"Failed to delete audio file {rec.audio_file_path}: {e}")
                    if action == "delete_record":
                        # Delete dependent telegram_alerts first (FK constraint)
                        await session.execute(
                            delete(TelegramAlert).where(TelegramAlert.recording_id == rec.id)
                        )
                        await session.delete(rec)
                    else:  # keep_transcript
                        rec.audio_file_path = None
                        rec.audio_file_name = None
                        rec.audio_deleted = 1

                await session.commit()
                logger.info(f"Retention enforcement COMPLETE ({len(to_remove)} affected)")
        except Exception as e:
            logger.error(f"Retention enforcement FAILED: {e}", exc_info=True)
            raise


# Global SDR manager instance
sdr_manager = SDRManager()


async def notify_live_tuner_changed(device_id: Optional[str] = None) -> None:
    """Tell the LIVE side that the shared tuner was retuned by a background job.

    A DeviceCapture (JOB) retunes the SHARED RtlTcpClient via its own
    consumer — bypassing SDRManager.set_frequency(). That leaves the LIVE
    consumer's mirror (freq/sample_rate/ppm) STALE and its VFO offset
    (relative to the OLD center) pointing outside the new passband. The
    symptom is the dashboard showing a listening frequency like 169.263 MHz
    while the real passband is 168.000–169.024 MHz → off-screen cursor and
    pure-noise audio.

    This function lets DeviceCapture notify the LIVE side so it can sync its
    mirror, reset/clamp its VFO offset, and reset every listener's offset.
    It is a best-effort, fire-and-forget call (all failures are logged, not
    raised) so a JOB's reclaim path can never be broken by the LIVE side.
    """
    try:
        mgr = sdr_manager
        # If a specific device_id is given and it does not match the active
        # LIVE device, there's nothing to sync.
        if device_id is not None and getattr(mgr, "_active_device_id", None) \
                and mgr._active_device_id != device_id:
            return
        await mgr._on_shared_tuner_changed()
    except Exception as e:
        logger.warning(f"notify_live_tuner_changed failed: {e}", exc_info=True)