"""
Recording Jobs API routes — CRUD for scheduled / always-on background recording.

Also exposes the admin-configurable server timezone used by the scheduler.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Any
import logging

from backend.auth.dependencies import get_current_user, require_admin
from backend.models.user import User
from backend.services import recording_jobs as jobs_service
from backend.services.recording_jobs import JobValidationError
from backend.services.app_settings import get_setting, set_setting

logger = logging.getLogger(__name__)

# Separate router for recording jobs (own prefix)
recording_jobs_router = APIRouter(prefix="/api/recording-jobs", tags=["Recording Jobs"])


# === Schemas ===

class FrequencyPayload(BaseModel):
    frequency_hz: int
    squelch_db: float = -70.0
    demod_mode: str = "FM"
    bandwidth_hz: int = 12000
    label: Optional[str] = None


class WindowPayload(BaseModel):
    start_time: Optional[str] = None  # HH:MM (scheduled only)
    end_time: Optional[str] = None    # HH:MM (scheduled only)
    days: Optional[Any] = None        # list[int] | str | None (0=Mon..6=Sun)
    gain: int = 30
    freq_correction_ppm: int = 0
    start_freq_hz: int
    sample_rate: int
    frequencies: List[FrequencyPayload]


class JobPayload(BaseModel):
    name: Optional[str] = None
    device_id: Optional[str] = None
    mode: str = "always"  # always | scheduled
    enabled: bool = True
    windows: Optional[List[WindowPayload]] = None
    # Inline single-window convenience fields (used when `windows` is omitted)
    gain: Optional[int] = None
    freq_correction_ppm: Optional[int] = None
    start_freq_hz: Optional[int] = None
    sample_rate: Optional[int] = None
    frequencies: Optional[List[FrequencyPayload]] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    days: Optional[Any] = None


class JobEnabledRequest(BaseModel):
    enabled: bool


def _job_to_dict(job) -> dict:
    d = job.to_dict()
    # Compute human-readable passband info for convenience
    if d.get("windows"):
        for w in d["windows"]:
            lo = w["start_freq_hz"]
            hi = lo + w["sample_rate"]
            w["passband_start_hz"] = lo
            w["passband_end_hz"] = hi
    return d


# === Recording Jobs CRUD ===

@recording_jobs_router.get("")
async def list_recording_jobs(current_user: User = Depends(get_current_user)):
    """List all recording jobs (any authenticated user can read)."""
    jobs = await jobs_service.list_jobs()
    return [_job_to_dict(j) for j in jobs]


@recording_jobs_router.get("/available-devices")
async def list_available_devices(current_user: User = Depends(get_current_user)):
    """List SDR devices available for a new recording job.

    Returns all devices + which ones are already used by enabled jobs, plus
    the set of used device ids, so the UI can disable/omit them.
    """
    from backend.models.sdr_device import SdrDevice
    from sqlalchemy import select
    from backend.database import async_session

    used = await jobs_service.get_used_device_ids()
    async with async_session() as session:
        result = await session.execute(
            select(SdrDevice).order_by(SdrDevice.created_at.asc())
        )
        devices = [d.to_dict() for d in result.scalars().all()]
    for d in devices:
        d["used_by_job"] = d["id"] in used
    return {
        "devices": devices,
        "used_device_ids": sorted(used),
        "available_count": sum(1 for d in devices if d["id"] not in used),
    }


@recording_jobs_router.post("")
async def create_recording_job(
    payload: JobPayload,
    current_user: User = Depends(require_admin),
):
    """Create a recording job (admin only)."""
    try:
        job = await jobs_service.create_job(payload.model_dump(exclude_none=False))
        _kick_scheduler()
        return _job_to_dict(job)
    except JobValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@recording_jobs_router.put("/{job_id}")
async def update_recording_job(
    job_id: str,
    payload: JobPayload,
    current_user: User = Depends(require_admin),
):
    """Update a recording job (admin only)."""
    try:
        job = await jobs_service.update_job(job_id, payload.model_dump(exclude_none=False))
        _kick_scheduler()
        return _job_to_dict(job)
    except JobValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@recording_jobs_router.delete("/{job_id}")
async def delete_recording_job(
    job_id: str,
    current_user: User = Depends(require_admin),
):
    """Delete a recording job (admin only)."""
    ok = await jobs_service.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="ไม่พบการตั้งค่าการอัดเสียง")
    _kick_scheduler()
    return {"success": True}


def _kick_scheduler():
    """Trigger an immediate scheduler re-evaluation (fire-and-forget)."""
    import asyncio
    from backend.sdr.job_scheduler import job_scheduler
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_safe_tick(job_scheduler))
    except RuntimeError:
        pass  # No running loop (shouldn't happen in request context)


async def _safe_tick(scheduler):
    """Run scheduler._tick() with error logging."""
    try:
        await scheduler._tick()
    except Exception as e:
        logger.warning(f"Scheduler immediate tick failed: {e}")


@recording_jobs_router.post("/{job_id}/enabled")
async def set_recording_job_enabled(
    job_id: str,
    req: JobEnabledRequest,
    current_user: User = Depends(require_admin),
):
    """Enable/disable a recording job (admin only)."""
    try:
        job = await jobs_service.set_job_enabled(job_id, req.enabled)
    except JobValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if job is None:
        raise HTTPException(status_code=404, detail="ไม่พบการตั้งค่าการอัดเสียง")
    _kick_scheduler()
    return _job_to_dict(job)


# === Runtime status (live capture state for dashboard) ===

@recording_jobs_router.get("/runtime-status")
async def get_runtime_status(current_user: User = Depends(get_current_user)):
    """Return live status of all active background captures."""
    from backend.sdr.job_scheduler import job_scheduler
    return job_scheduler.runtime_status()


# === Server timezone (admin-configurable, used by scheduler) ===

@recording_jobs_router.get("/server-timezone")
async def get_server_timezone(current_user: User = Depends(get_current_user)):
    raw = await get_setting("server_timezone", "Asia/Bangkok")
    return {"timezone": raw or "Asia/Bangkok"}


class ServerTimezoneRequest(BaseModel):
    timezone: str


@recording_jobs_router.put("/server-timezone")
async def set_server_timezone(
    req: ServerTimezoneRequest,
    current_user: User = Depends(require_admin),
):
    tz = (req.timezone or "").strip()
    if not tz:
        raise HTTPException(status_code=400, detail="timezone ต้องไม่ว่าง")
    # Validate the timezone exists
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        ZoneInfo(tz)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"timezone ไม่ถูกต้อง: {e}")
    await set_setting("server_timezone", tz)
    logger.info(f"Server timezone set to {tz} by {current_user.username}")
    return {"timezone": tz}