"""
Live Runtime Manager — multi-device live listening.

Provides per-device SDRManager instances so that different browser tabs /
users can listen to DIFFERENT physical SDR devices simultaneously and
independently (different center frequency, gain, mode, waterfall, audio).

Architecture
------------
- Each registered device_id owns its own SDRManager + AudioBroadcaster pair.
- The legacy global ``sdr_manager`` singleton remains as the DEFAULT runtime
  (used when no device_id is specified, for backward compatibility).
- The legacy global ``audio_broadcaster`` singleton is used by the default
  runtime only.
- Per-device (non-default) runtimes create their own AudioBroadcaster so
  listeners, IQ fan-out, and per-listener demodulators are fully isolated.

Usage from endpoints
--------------------
    from backend.sdr.live_runtime import live_runtimes
    mgr, bcast = await live_runtimes.get(device_id)
    # mgr is an SDRManager, bcast is an AudioBroadcaster

The frontend selects a device per tab and passes ?device_id=... on every
API call and WebSocket connection.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Tuple

from backend.audio.broadcaster import AudioBroadcaster
from backend.sdr.manager import SDRManager, sdr_manager
from backend.audio.broadcaster import audio_broadcaster as _global_broadcaster
from backend.sdr.vfo_limit import VFO_MARGIN_DEFAULT

logger = logging.getLogger(__name__)


def _global_broadcaster_manager_ratio() -> float:
    """Current VFO passband ratio from the DEFAULT (global) runtime.

    Per-device runtimes must use the same admin-configured policy as the
    default one; otherwise the tuning range silently differs per device.
    """
    return getattr(sdr_manager, "_vfo_margin_ratio", VFO_MARGIN_DEFAULT)


class LiveRuntimeManager:
    """Registry of per-device live runtimes (SDRManager + AudioBroadcaster)."""

    def __init__(self):
        self._runtimes: Dict[str, Tuple[SDRManager, AudioBroadcaster]] = {}
        self._lock = asyncio.Lock()

    async def get(
        self, device_id: Optional[str] = None
    ) -> Tuple[SDRManager, AudioBroadcaster]:
        """Return the (SDRManager, AudioBroadcaster) for a device.

        If device_id is None or matches the active device on the global
        singleton, the legacy global pair is returned (backward compatible).

        Otherwise a dedicated runtime is created (lazily) and cached.
        """
        # Default runtime (no device specified) → global singleton pair.
        if not device_id:
            return sdr_manager, _global_broadcaster

        # If the requested device is the one the global singleton already
        # manages, reuse it so existing behavior is unchanged.
        if getattr(sdr_manager, "_active_device_id", None) == device_id:
            return sdr_manager, _global_broadcaster

        async with self._lock:
            existing = self._runtimes.get(device_id)
            if existing is not None:
                return existing

            # Lazily create a new isolated runtime for this device.
            mgr, bcast = await self._create_runtime(device_id)
            self._runtimes[device_id] = (mgr, bcast)
            return mgr, bcast

    async def _create_runtime(
        self, device_id: str
    ) -> Tuple[SDRManager, AudioBroadcaster]:
        """Create a fresh SDRManager + AudioBroadcaster bound to a device."""
        from backend.models.sdr_device import SdrDevice
        from backend.database import async_session
        from sqlalchemy import select

        # Load the device row to get its config.
        async with async_session() as session:
            device = await session.get(SdrDevice, device_id)
            if device is None:
                raise ValueError(f"Device not found: {device_id}")
            device_dict = device.to_dict()

        # Create a dedicated AudioBroadcaster (isolated listeners / IQ fan-out).
        bcast = AudioBroadcaster()
        # Inherit the global max_listeners + auto-record policy.
        bcast.set_max_listeners(_global_broadcaster.max_listeners)
        bcast.ar_vad_silence_timeout = _global_broadcaster.ar_vad_silence_timeout
        bcast.ar_max_duration = _global_broadcaster.ar_max_duration
        bcast.ar_min_duration = _global_broadcaster.ar_min_duration
        bcast.vfo_margin_ratio = _global_broadcaster.vfo_margin_ratio

        # Create a dedicated SDRManager bound to this broadcaster + device.
        mgr = SDRManager(broadcaster=bcast)
        mgr._active_device_id = device_id
        # Inherit the admin's VFO passband policy. Without this a tab that
        # listens on a NON-default device kept SDRManager's hardcoded 0.40
        # default, so its tuning range was ±819 kHz at 2.048 MSPS while the
        # default runtime allowed ±1003 kHz — and the UI warned at yet another
        # value (see backend/sdr/vfo_limit.py).
        mgr._vfo_margin_ratio = _global_broadcaster_manager_ratio()
        # Apply the device's config (host/port/type/usb params + tuner defaults).
        mgr._apply_device_config_by_dict(device_dict)

        # NOTE: auto-record + listener-count callbacks are already wired inside
        # SDRManager.__init__ (which binds to self._broadcaster == bcast).
        # Do NOT register them again here — that would double-register the same
        # callback and cause every auto-record segment to be saved TWICE
        # (duplicate rows in the recordings list).
        mgr._sync_auto_record_policy()

        logger.info(
            f"🆕 LiveRuntime created for device {device_id} "
            f"({device_dict.get('name')})"
        )
        return mgr, bcast

    async def remove(self, device_id: str) -> None:
        """Tear down and remove a per-device runtime (e.g. on device delete)."""
        async with self._lock:
            pair = self._runtimes.pop(device_id, None)
        if pair is None:
            return
        mgr, bcast = pair
        try:
            await mgr.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting runtime for {device_id}: {e}")

    def list_device_ids(self) -> list[str]:
        """Return the list of device_ids with active dedicated runtimes."""
        return list(self._runtimes.keys())

    def all_runtimes(self) -> Dict[str, Tuple[SDRManager, AudioBroadcaster]]:
        """Return a snapshot of all dedicated runtimes (excludes the default)."""
        return dict(self._runtimes)


# Global singleton registry.
live_runtimes = LiveRuntimeManager()