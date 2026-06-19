"""
JobScheduler — orchestrates background DeviceCapture workers.

Responsibilities:
  - Load all enabled RecordingJobs from DB.
  - For each job, determine whether it should be active NOW:
      * always-mode → always active (single window)
      * scheduled-mode → active if current server-time falls within any window's
        [start_time, end_time] and day-of-week matches (if specified).
  - Start/stop DeviceCapture workers as windows activate/deactivate.
  - Auto-retry on capture crashes.
  - Expose runtime status for the dashboard API.

Concurrency model:
  - Each enabled job owns exactly ONE device (enforced at job-create time).
  - Each active job has exactly one DeviceCapture.
  - DeviceCapture connects to its OWN rtl_tcp instance (separate from the
    interactive SDRManager). This means:
      * background recording never retunes the device the user is listening to.
      * if the SAME physical rtl_tcp endpoint is shared (same host:port), the
        last tuner command wins — so in practice background jobs should use
        DEDICATED rtl_tcp instances (e.g. separate rtl_tcp processes or
        separate physical SDR dongles). The UI enforces one-job-per-device.
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import async_session
from backend.models.recording_job import (
    RecordingJob, RecordingJobWindow, RecordingJobMode,
)
from backend.services.app_settings import get_setting
from backend.sdr.device_capture import DeviceCapture


logger = logging.getLogger(__name__)

# How often the scheduler re-evaluates which jobs should be active.
_TICK_INTERVAL = 30  # seconds

# Default recording params (mirrored from SDRManager defaults; could be made
# configurable via app settings later).
_DEFAULT_SILENCE_TIMEOUT = 5
_DEFAULT_MAX_REC_DURATION = 300
_DEFAULT_MIN_REC_DURATION = 2.0


class JobScheduler:
    """Singleton scheduler that manages all background DeviceCapture workers."""

    def __init__(self):
        # job_id → DeviceCapture (currently running)
        self._captures: Dict[str, DeviceCapture] = {}
        self._tick_task: Optional[asyncio.Task] = None
        self._server_tz: str = "Asia/Bangkok"
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the scheduler tick loop (called from app lifespan)."""
        if self._started:
            return
        self._started = True
        await self._load_timezone()
        logger.info(f"🕐 JobScheduler started (tz={self._server_tz})")
        # Do an immediate evaluation, then schedule periodic ticks
        await self._tick()
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """Stop all captures + the tick loop (called from app shutdown)."""
        self._started = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        # Stop all active captures
        await self._stop_all_captures()
        logger.info("🕐 JobScheduler stopped")

    async def _stop_all_captures(self) -> None:
        stops = [cap.stop() for cap in self._captures.values()]
        if stops:
            await asyncio.gather(*stops, return_exceptions=True)
        self._captures.clear()

    async def _load_timezone(self) -> None:
        try:
            self._server_tz = await get_setting("server_timezone", "Asia/Bangkok")
        except Exception as e:
            logger.warning(f"Failed to load server timezone, using default: {e}")
            self._server_tz = "Asia/Bangkok"

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------
    async def _tick_loop(self) -> None:
        try:
            while self._started:
                await asyncio.sleep(_TICK_INTERVAL)
                try:
                    await self._tick()
                except Exception as e:
                    logger.error(f"JobScheduler tick error: {e}", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        """Re-evaluate which jobs should be active and start/stop captures."""
        # Reload timezone in case admin changed it
        await self._load_timezone()
        jobs = await self._load_enabled_jobs()
        now = self._server_now()
        active_job_ids: set = set()

        for job in jobs:
            window = self._active_window_for_job(job, now)
            if window is None:
                continue
            active_job_ids.add(job.id)

            # Already running?
            existing = self._captures.get(job.id)
            if existing and existing.is_running and existing.window_id == window.id:
                continue  # still active, same window → no change

            # Need to (re)start: stop old capture if window changed
            if existing:
                try:
                    await existing.stop()
                except Exception as e:
                    logger.warning(f"Error stopping old capture for job {job.id}: {e}")
                del self._captures[job.id]

            # Start new capture
            try:
                cap = DeviceCapture(
                    job=job,
                    window=window,
                    silence_timeout=_DEFAULT_SILENCE_TIMEOUT,
                    max_recording_duration=_DEFAULT_MAX_REC_DURATION,
                    min_recording_duration=_DEFAULT_MIN_REC_DURATION,
                )
                await cap.start()
                self._captures[job.id] = cap
                # NOTE: No preemption callback needed — DeviceCapture now
                # dynamically recomputes per-frequency offsets from the
                # actual center frequency and keeps recording as long as
                # the target is within the current passband.
            except Exception as e:
                logger.error(f"Failed to start DeviceCapture for job {job.id}: {e}")

        # Stop captures whose jobs are no longer active
        to_stop = [jid for jid in self._captures if jid not in active_job_ids]
        for jid in to_stop:
            cap = self._captures.pop(jid, None)
            if cap:
                try:
                    await cap.stop()
                    logger.info(f"📼 Stopped capture for job {jid} (no longer active)")
                except Exception as e:
                    logger.warning(f"Error stopping capture for job {jid}: {e}")

    # ------------------------------------------------------------------
    # Job/window evaluation
    # ------------------------------------------------------------------
    async def _load_enabled_jobs(self) -> List[RecordingJob]:
        """Load all enabled RecordingJobs with windows+frequencies eagerly loaded."""
        async with async_session() as session:
            result = await session.execute(
                select(RecordingJob)
                .options(
                    selectinload(RecordingJob.windows)
                    .selectinload(RecordingJobWindow.frequencies)
                )
                .where(RecordingJob.enabled == True)  # noqa: E712
            )
            return list(result.scalars().all())

    def _active_window_for_job(
        self, job: RecordingJob, now: datetime
    ) -> Optional[RecordingJobWindow]:
        """Return the window that should be active NOW, or None."""
        windows = job.windows or []
        if not windows:
            return None

        if job.mode == RecordingJobMode.ALWAYS.value:
            # Always-mode has exactly one window (no time/day constraints)
            return windows[0]

        # Scheduled-mode: check each window
        now_hhmm = now.strftime("%H:%M")
        # JS-style weekday: 0=Sun..6=Sat → our DB 0=Mon..6=Sun
        js_dow = now.weekday()  # Python: 0=Mon..6=Sun (matches our DB!)
        # Actually Python weekday() IS 0=Mon..6=Sun, same as our DB convention.

        for w in windows:
            if not w.start_time or not w.end_time:
                continue
            if now_hhmm < w.start_time or now_hhmm > w.end_time:
                continue
            if w.days_of_week:
                days = [int(d.strip()) for d in w.days_of_week.split(",") if d.strip().isdigit()]
                if js_dow not in days:
                    continue
            return w
        return None

    def _server_now(self) -> datetime:
        """Return current datetime in the server timezone."""
        try:
            tz = ZoneInfo(self._server_tz)
            return datetime.now(tz)
        except Exception:
            return datetime.now()

    # ------------------------------------------------------------------
    # Public API for status reporting
    # ------------------------------------------------------------------
    def runtime_status(self) -> dict:
        """Return the current state of all active captures."""
        return {
            "server_timezone": self._server_tz,
            "server_time": self._server_now().isoformat(),
            "active_captures": [
                cap.runtime_status() for cap in self._captures.values()
            ],
            "active_job_ids": sorted(self._captures.keys()),
        }

    def get_capture(self, job_id: str) -> Optional[DeviceCapture]:
        return self._captures.get(job_id)


# Global singleton
job_scheduler = JobScheduler()