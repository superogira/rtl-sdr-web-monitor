"""
SDR API Routes - control RTL-TCP, streaming, recording.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from pathlib import Path as FSPath
import os
import logging

from backend.auth.dependencies import get_current_user, require_admin
from backend.models.user import User
from backend.sdr.manager import sdr_manager
from backend.sdr.live_runtime import live_runtimes
from backend.audio.demod import DemodMode
from backend.database import get_db
from backend.models.recording import Recording, RecordingStatus
from backend.config import settings
from backend.services.stt import transcribe, _get_stt_config, list_maxplus_models
from backend.services.app_settings import get_setting, set_setting, set_settings_batch
from backend.models.app_setting import mask_value, is_masked_value
from backend.models.sdr_device import SdrDevice, DEVICE_TYPES, default_config_for_type
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _get_runtime(device_id: Optional[str] = None):
    """Resolve the live runtime (SDRManager, AudioBroadcaster) for a device.

    Falls back to the global singleton when device_id is None or matches the
    active device on the global singleton (backward compatible).
    """
    return await live_runtimes.get(device_id)


router = APIRouter(prefix="/api/sdr", tags=["SDR Control"])

# Separate router for recordings (different prefix)
recordings_router = APIRouter(prefix="/api/recordings", tags=["Recordings"])

# Router for global app settings (STT config, etc.)
settings_router = APIRouter(prefix="/api/settings", tags=["App Settings"])

# Router for SDR device management (registry + active device)
devices_router = APIRouter(prefix="/api/sdr/devices", tags=["SDR Devices"])


# === Schemas ===

class FrequencyRequest(BaseModel):
    frequency: int  # Hz

class SampleRateRequest(BaseModel):
    sample_rate: int  # Hz

class GainRequest(BaseModel):
    gain: int  # dB

class ModeRequest(BaseModel):
    mode: str  # FM, AM, USB, LSB

class SquelchRequest(BaseModel):
    level: float  # dB

class FFTSizeRequest(BaseModel):
    fft_size: int  # Must be power of 2 (256-8192)

class BandwidthRequest(BaseModel):
    bandwidth: int  # Hz (channel bandwidth, 1kHz-500kHz)

class FreqCorrectionRequest(BaseModel):
    ppm: int  # Parts per million frequency correction

class ConnectionRequest(BaseModel):
    host: str
    port: int = 1234


class RecordingConfigRequest(BaseModel):
    """Global recording config (shared by all users)."""
    auto_record: Optional[bool] = None
    squelch_level: Optional[float] = None
    vad_silence_timeout: Optional[int] = None  # seconds
    max_recording_duration: Optional[int] = None  # seconds
    min_recording_duration: Optional[int] = None  # seconds
    retention_max_count: Optional[int] = None  # 0 = unlimited
    retention_max_total_duration_sec: Optional[int] = None  # 0 = unlimited
    retention_max_age_days: Optional[int] = None  # 0 = unlimited
    retention_action: Optional[str] = None  # keep_transcript | delete_record


# === Endpoints ===

@router.get("/status")
async def get_sdr_status(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Get current SDR status.

    Optional ?device_id= selects a per-device live runtime so different browser
    tabs can listen to different physical SDR devices independently.
    """
    mgr, _ = await _get_runtime(device_id)
    return mgr.status


@router.post("/connection")
async def set_connection(
    req: ConnectionRequest,
    current_user: User = Depends(get_current_user),
):
    """Set RTL-TCP host/port (persisted; used on next connect)."""
    return sdr_manager.set_connection(req.host, req.port)



@router.post("/connect")
async def connect_sdr(
    req: Optional[ConnectionRequest] = None,
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Connect to RTL-TCP server.

    Optionally accepts {host, port} to update the target before connecting.
    Pass ?device_id= to connect a per-device runtime (multi-device listening).
    """
    mgr, _ = await _get_runtime(device_id)
    if req is not None and req.host:
        # If currently connected, disconnect first so new endpoint applies.
        if mgr.client.connected:
            await mgr.disconnect()
        mgr.set_connection(req.host, req.port)
    result = await mgr.connect()
    if not result["success"]:
        # Include the staged reason (network vs rtl_tcp busy) so the UI can
        # tell the operator what to check instead of a generic failure.
        detail = result.get("error") or "Failed to connect to RTL-TCP"
        raise HTTPException(status_code=503, detail=detail)
    return result


@router.post("/disconnect")
async def disconnect_sdr(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Disconnect from RTL-TCP."""
    mgr, _ = await _get_runtime(device_id)
    await mgr.disconnect()
    return {"success": True}


@router.post("/frequency")
async def set_frequency(
    req: FrequencyRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(require_admin),
):
    """Set center frequency (admin only).
    Pass ?device_id= for per-device live runtime.
    """
    mgr, _ = await _get_runtime(device_id)
    if not mgr.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    await mgr.set_frequency(req.frequency)
    return {"frequency": req.frequency}


class VFORequest(BaseModel):
    frequency: int  # Hz (absolute frequency to listen at)


@router.post("/vfo")
async def set_vfo(
    req: VFORequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Set the VFO (listening) frequency by absolute Hz.
    Pass ?device_id= for per-device live runtime.
    """
    mgr, _ = await _get_runtime(device_id)
    if not mgr.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    try:
        return mgr.set_vfo_frequency(req.frequency)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class VFOMarginRequest(BaseModel):
    ratio: float  # 0.40–0.49


@router.post("/vfo-margin")
async def set_vfo_margin(
    req: VFOMarginRequest,
    current_user: User = Depends(require_admin),
):
    """Set the VFO passband margin ratio (admin only).

    Controls how far the VFO can tune from center:
      max_offset = sample_rate * ratio
    Accepts 0.40–0.49. Lower = safer (less aliasing near band edge),
    higher = wider tunable range.
    """
    try:
        return await sdr_manager.set_vfo_margin_ratio(req.ratio)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class MaxListenersRequest(BaseModel):
    max_listeners: int  # 1–128


@router.get("/max-listeners")
async def get_max_listeners(current_user: User = Depends(require_admin)):
    """Get the max concurrent listeners setting (admin only)."""
    from backend.audio.broadcaster import audio_broadcaster
    return {
        "max_listeners": audio_broadcaster.max_listeners,
        "current_listeners": audio_broadcaster.listener_count,
        "hard_cap": sdr_manager._HARD_CAP_LISTENERS,
    }


@router.put("/max-listeners")
async def set_max_listeners(
    req: MaxListenersRequest,
    current_user: User = Depends(require_admin),
):
    """Set the max concurrent listeners (admin only).

    Controls how many users can listen to live audio simultaneously. When the
    cap is reached, new listeners receive a "full" message. Accepts 1–128.
    Default is 8; scale up on powerful servers (each listener runs a demod
    thread).
    """
    return await sdr_manager.set_max_listeners(req.max_listeners)


@router.get("/capabilities")
async def get_capabilities(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Get supported sample rates and gains for the connected device."""
    mgr, _ = await _get_runtime(device_id)
    if not mgr.client or not mgr.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    try:
        caps = mgr.client.get_capabilities()
        return caps
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sample-rate")
async def set_sample_rate(
    req: SampleRateRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(require_admin),
):
    """Set sample rate (admin only)."""
    mgr, _ = await _get_runtime(device_id)
    if not mgr.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    await mgr.set_sample_rate(req.sample_rate)
    return {"sample_rate": req.sample_rate}


@router.post("/gain")
async def set_gain(
    req: GainRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(require_admin),
):
    """Set gain (admin only)."""
    mgr, _ = await _get_runtime(device_id)
    if not mgr.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    await mgr.set_gain(req.gain)
    return {"gain": req.gain}


@router.post("/mode")
async def set_mode(
    req: ModeRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Set demodulation mode."""
    mgr, _ = await _get_runtime(device_id)
    try:
        await mgr.set_mode(req.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {req.mode}. Use FM, AM, USB, LSB")
    return {"mode": req.mode.upper()}


@router.post("/squelch")
async def set_squelch(
    req: SquelchRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(require_admin),
):
    """Set the GLOBAL squelch baseline (admin only).

    This controls the global auto-record gating threshold and the initial
    squelch given to newly-connected listeners. Regular users adjust their
    OWN per-listener squelch via the audio WebSocket ({"type":"squelch"}),
    which is fully independent — one user changing SQL no longer moves
    anyone else's slider.
    """
    mgr, _ = await _get_runtime(device_id)
    mgr.set_squelch(req.level)
    return {"squelch": req.level}


@router.post("/fft-size")
async def set_fft_size(
    req: FFTSizeRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Set FFT output bins (waterfall resolution)."""
    mgr, _ = await _get_runtime(device_id)
    try:
        mgr.set_fft_bins(req.fft_size)
        return {"fft_size": req.fft_size}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bandwidth")
async def set_bandwidth(
    req: BandwidthRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Set channel bandwidth (Hz) for signal level computation."""
    mgr, _ = await _get_runtime(device_id)
    try:
        mgr.set_channel_bw(req.bandwidth)
        return {"bandwidth": req.bandwidth}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/freq-correction")
async def set_freq_correction(
    req: FreqCorrectionRequest,
    device_id: Optional[str] = None,
    current_user: User = Depends(require_admin),
):
    """Set frequency correction in PPM (admin only)."""
    mgr, _ = await _get_runtime(device_id)
    await mgr.set_freq_correction(req.ppm)
    return {"ppm": req.ppm}


@router.get("/recording-config")
async def get_recording_config(current_user: User = Depends(require_admin)):
    """Get global recording config (admin only; config affects all users)."""
    return sdr_manager.get_recording_config()


@router.post("/recording-config")
async def set_recording_config(
    req: RecordingConfigRequest,
    current_user: User = Depends(require_admin),
):
    """Update global recording config (persisted across restarts)."""
    try:
        return sdr_manager.set_recording_config(
            auto_record=req.auto_record,
            squelch_level=req.squelch_level,
            vad_silence_timeout=req.vad_silence_timeout,
            max_recording_duration=req.max_recording_duration,
            min_recording_duration=req.min_recording_duration,
            retention_max_count=req.retention_max_count,
            retention_max_total_duration_sec=req.retention_max_total_duration_sec,
            retention_max_age_days=req.retention_max_age_days,
            retention_action=req.retention_action,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class RecordingEnabledRequest(BaseModel):
    """Toggle to enable/disable manual recording for role=user users."""
    enabled: bool


@router.get("/recording-enabled")
async def get_recording_enabled(current_user: User = Depends(get_current_user)):
    """Check if manual recording is enabled (admins always have access).

    Returns {enabled: true, admin_bypass: true}.
    """
    if current_user.is_admin:
        return {"enabled": True, "admin_bypass": True}
    raw = await get_setting("manual_recording_enabled", "true")
    return {"enabled": str(raw).lower() == "true", "admin_bypass": False}


@router.post("/recording-enabled")
async def set_recording_enabled(
    req: RecordingEnabledRequest,
    current_user: User = Depends(require_admin),
):
    """Enable or disable manual recording for regular (role=user) users.

    Admins are never affected by this toggle — they can always record.
    """
    await set_setting("manual_recording_enabled", "true" if req.enabled else "false")
    logger.info(
        f"Manual recording for role=user set to {req.enabled} by {current_user.username}"
    )
    return {"enabled": req.enabled}


@router.post("/stream/start")
async def start_streaming(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Start streaming IQ data."""
    mgr, _ = await _get_runtime(device_id)
    result = await mgr.start_streaming()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/stream/stop")
async def stop_streaming(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Stop streaming."""
    mgr, _ = await _get_runtime(device_id)
    return await mgr.stop_streaming()


@router.post("/record/start")
async def start_recording(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Start recording manually."""
    mgr, _ = await _get_runtime(device_id)
    result = await mgr.start_recording()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/record/stop")
async def stop_recording(
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Stop recording."""
    mgr, _ = await _get_runtime(device_id)
    result = await mgr.stop_recording()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# === Recordings endpoints (from DB) ===

FREQ_TOLERANCE_KHZ = 5.0  # tolerance สำหรับ frequency filter (±5 kHz)


def _apply_recording_filters(stmt, source, search, frequency_mhz, device_id):
    """Apply the shared filter set (source/device/frequency/search) to a query.

    Centralizes filter logic so the list/count/sum queries stay in sync and
    we avoid running 3 separate filtered queries (was list + count + sum).
    """
    if source:
        stmt = stmt.where(Recording.source == source)
    if device_id:
        stmt = stmt.where(Recording.device_id == device_id)
    if frequency_mhz is not None:
        tol_hz = FREQ_TOLERANCE_KHZ * 1e3
        center_hz = frequency_mhz * 1e6
        stmt = stmt.where(
            Recording.frequency.between(center_hz - tol_hz, center_hz + tol_hz)
        )
    if search:
        s = f"%{search}%"
        # frequency search by kHz (e.g. "145" matches 145500000 Hz)
        freq_search = None
        try:
            freq_search = float(search) * 1e6
        except (ValueError, TypeError):
            pass
        clauses = [
            Recording.transcript.ilike(s),
            Recording.audio_file_name.ilike(s),
            Recording.mode.ilike(s),
        ]
        if freq_search is not None:
            clauses.append(Recording.frequency == freq_search)
        stmt = stmt.where(or_(*clauses))
    return stmt


@recordings_router.get("")
async def list_recordings(
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,
    search: Optional[str] = None,
    frequency_mhz: Optional[float] = None,
    device_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """List recordings from database (paginated, newest first).

    Optional filters:
    - source: "auto" | "manual" | "auto_job"
    - search: case-insensitive substring match on transcript, audio_file_name, mode, frequency (kHz)
    - frequency_mhz: filter by center frequency with ±5 kHz tolerance (e.g. 145.5 matches 145.4975–145.5025 MHz)
    - device_id: filter by SDR device ID
    """
    # --- Page of rows ---
    rows_q = _apply_recording_filters(
        select(Recording).order_by(Recording.recorded_at.desc()),
        source, search, frequency_mhz, device_id,
    ).limit(limit).offset(offset)
    recordings = (await db.execute(rows_q)).scalars().all()

    # --- Total + total_duration + total_file_size in ONE aggregated query ---
    # (Previously this ran two separate queries with duplicated filter code.)
    agg_q = _apply_recording_filters(
        select(
            func.count(Recording.id),
            func.coalesce(func.sum(Recording.duration), 0.0),
            func.coalesce(func.sum(Recording.file_size), 0),
        ),
        source, search, frequency_mhz, device_id,
    )
    total, total_duration, total_file_size = (await db.execute(agg_q)).one()

    return {
        "items": [r.to_dict() for r in recordings],
        "total": total,
        "total_duration": round(total_duration or 0.0, 1),
        "total_file_size": int(total_file_size or 0),
        "limit": limit,
        "offset": offset,
    }


@recordings_router.get("/meta/frequencies")
async def list_frequencies(
    source: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Return distinct frequencies with recording counts (for filter dropdown).

    Groups recordings within ±5 kHz tolerance so that drift-friendly buckets
    appear as a single option.
    """
    query = select(
        Recording.frequency,
        Recording.frequency_mhz,
        func.count(Recording.id).label("count"),
    ).group_by(Recording.frequency, Recording.frequency_mhz)
    if source:
        query = query.where(Recording.source == source)
    query = query.order_by(Recording.frequency.asc())
    result = await db.execute(query)
    rows = result.all()

    # Bucket ความถี่ที่ใกล้กัน (tolerance ±5 kHz) เป็นก้อนเดียว
    tol_mhz = FREQ_TOLERANCE_KHZ / 1000.0
    buckets = []  # [{frequency_mhz, count, min_mhz, max_mhz}]
    for freq_hz, freq_mhz, count in rows:
        mhz = freq_mhz if freq_mhz is not None else (freq_hz / 1e6 if freq_hz else None)
        if mhz is None:
            continue
        merged = False
        for b in buckets:
            if abs(mhz - b["frequency_mhz"]) <= tol_mhz:
                b["count"] += count
                b["min_mhz"] = min(b["min_mhz"], mhz)
                b["max_mhz"] = max(b["max_mhz"], mhz)
                # ถ่วงน้ำหนักค่ากลางด้วย count
                total = b["count"]
                b["frequency_mhz"] = (
                    (b["frequency_mhz"] * (total - count)) + (mhz * count)
                ) / total
                merged = True
                break
        if not merged:
            buckets.append({
                "frequency_mhz": mhz,
                "count": count,
                "min_mhz": mhz,
                "max_mhz": mhz,
            })

    # เรียงตามความถี่
    buckets.sort(key=lambda b: b["frequency_mhz"])
    # ปัดทศนิยมให้สวย
    for b in buckets:
        b["frequency_mhz"] = round(b["frequency_mhz"], 4)
        b["min_mhz"] = round(b["min_mhz"], 4)
        b["max_mhz"] = round(b["max_mhz"], 4)
    return {"items": buckets, "tolerance_khz": FREQ_TOLERANCE_KHZ}


@recordings_router.get("/meta/stt-stats")
async def stt_usage_stats(
    start: Optional[str] = None,
    end: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Aggregate transcribed audio length over a date range.

    Returns two breakdowns (minutes, summed from audio `duration` seconds):
      - by_provider: total transcribed audio length per STT provider
      - by_whisper_model: total transcribed audio length per Whisper model name
        (provider=whisper_local/groq/maxplus), plus the wait/audio ratio (RTF)

    Query params:
      - start: ISO 8601 datetime (inclusive). Defaults to 30 days ago.
      - end:   ISO 8601 datetime (inclusive). Defaults to now (UTC).
    """
    from datetime import datetime, timezone, timedelta

    def _parse_dt(value, default):
        if not value:
            return default
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return default

    now = datetime.now(timezone.utc)
    start_dt = _parse_dt(start, now - timedelta(days=30))
    end_dt = _parse_dt(end, now)

    base_filter = [
        Recording.stt_duration.is_not(None),
        Recording.stt_provider.is_not(None),
        Recording.recorded_at >= start_dt,
        Recording.recorded_at <= end_dt,
    ]

    # --- by provider ---
    # total_minutes here = total TRANSCRIBED AUDIO LENGTH (sum of recording
    # duration), NOT the time spent transcribing.
    q_provider = (
        select(
            Recording.stt_provider,
            func.coalesce(func.sum(Recording.duration), 0.0).label("total_audio_seconds"),
            func.sum(Recording.stt_duration).label("total_stt_seconds"),
            func.count(Recording.id).label("count"),
        )
        .where(*base_filter)
        .group_by(Recording.stt_provider)
    )
    rows = (await db.execute(q_provider)).all()
    by_provider = [
        {
            "provider": (p or "unknown"),
            "total_minutes": round(float(audio_secs or 0.0) / 60.0, 2),
            "total_audio_seconds": round(float(audio_secs or 0.0), 1),
            "total_stt_seconds": round(float(stt_secs or 0.0), 1),
            "count": int(cnt or 0),
        }
        for (p, audio_secs, stt_secs, cnt) in rows
    ]
    by_provider.sort(key=lambda x: x["total_minutes"], reverse=True)

    # --- by whisper model (Whisper Local + Groq/MaxPlus hosted Whisper) ---
    # Also include audio duration sum for the wait/length ratio (RTF).
    q_model = (
        select(
            Recording.stt_model,
            func.sum(Recording.stt_duration).label("total_seconds"),
            func.coalesce(func.sum(Recording.duration), 0.0).label("total_audio_seconds"),
            func.count(Recording.id).label("count"),
        )
        .where(*base_filter, Recording.stt_provider.in_(["whisper_local", "groq", "maxplus"]))
        .group_by(Recording.stt_model)
    )
    mrows = (await db.execute(q_model)).all()
    by_whisper_model = []
    for (m, secs, audio_secs, cnt) in mrows:
        secs = secs or 0.0
        audio_secs = float(audio_secs or 0.0)
        # Weighted ratio (RTF) = total wait time / total audio length.
        # Avoids division by zero per row and reflects the true overall wait.
        ratio = round(secs / audio_secs, 3) if audio_secs > 0 else None
        by_whisper_model.append({
            "model": (m or "unknown"),
            "total_minutes": round(audio_secs / 60.0, 2),
            "count": int(cnt or 0),
            "total_audio_seconds": round(audio_secs, 1),
            "total_stt_seconds": round(float(secs), 1),
            "ratio": ratio,
        })
    by_whisper_model.sort(key=lambda x: x["total_minutes"], reverse=True)

    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "by_provider": by_provider,
        "by_whisper_model": by_whisper_model,
    }


@recordings_router.get("/{rec_id}")
async def get_recording(
    rec_id: str,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Get a single recording by ID."""
    result = await db.execute(select(Recording).where(Recording.id == rec_id))
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    return rec.to_dict()


@recordings_router.delete("/{rec_id}")
async def delete_recording(
    rec_id: str,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Delete a recording (DB row + audio file). Admin only."""
    result = await db.execute(select(Recording).where(Recording.id == rec_id))
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    # Delete audio file from disk
    if rec.audio_file_path:
        try:
            fpath = FSPath(rec.audio_file_path)
            if fpath.exists():
                os.remove(fpath)
        except Exception as e:
            pass  # log but don't fail

    await db.delete(rec)
    await db.commit()
    return {"success": True, "id": rec_id}


class BulkDeleteRequest(BaseModel):
    """Bulk delete recordings by IDs (admin only)."""
    ids: list[str]


@recordings_router.post("/bulk-delete")
async def bulk_delete_recordings(
    req: BulkDeleteRequest,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Delete multiple recordings (DB rows + audio files) in one call. Admin only.

    Accepts { ids: ["..."] }. Guards against abuse with a max batch size.
    Returns counts + which IDs were deleted vs. not found.
    """
    MAX_BATCH = 1000
    ids = req.ids or []
    if not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    # dedupe + cap size
    unique_ids = list(dict.fromkeys(str(i) for i in ids))
    if len(unique_ids) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"Too many ids (max {MAX_BATCH} per request, got {len(unique_ids)})",
        )

    result = await db.execute(select(Recording).where(Recording.id.in_(unique_ids)))
    recs = result.scalars().all()
    found_ids = []
    for rec in recs:
        # Delete audio file from disk (best-effort)
        if rec.audio_file_path:
            try:
                fpath = FSPath(rec.audio_file_path)
                if fpath.exists():
                    os.remove(fpath)
            except Exception:
                pass
        found_ids.append(rec.id)
        await db.delete(rec)
    await db.commit()

    found_set = set(found_ids)
    not_found_ids = [i for i in unique_ids if i not in found_set]
    logger.info(
        f"Bulk delete by {current_user.username}: requested={len(unique_ids)} "
        f"deleted={len(found_ids)} not_found={len(not_found_ids)}"
    )
    return {
        "success": True,
        "requested_count": len(unique_ids),
        "deleted_count": len(found_ids),
        "deleted_ids": found_ids,
        "not_found_ids": not_found_ids,
    }


@recordings_router.post("/{rec_id}/read")
async def mark_as_read(
    rec_id: str,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Mark a recording as read (is_unread=0)."""
    result = await db.execute(select(Recording).where(Recording.id == rec_id))
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    rec.is_unread = 0
    await db.commit()
    return {"success": True, "id": rec_id, "is_unread": 0}


class QualityRatingRequest(BaseModel):
    """User verdict on how good a transcription is (1-6 scale).

    1 = แย่มาก (worst), 6 = ดีที่สุด (best).
    """
    rating: int  # 1..6


# Quality rating metadata: 1 (worst) → 6 (best).
# Maps the integer score to a Thai label + emoji used by the UI. Defined here
# as the single source of truth for the backend; the frontend mirrors it.
_QUALITY_RATING_META = {
    1: {"label": "แย่มาก", "emoji": "😡"},
    2: {"label": "แย่",     "emoji": "👎"},
    3: {"label": "พอใช้",   "emoji": "😐"},
    4: {"label": "ดี",       "emoji": "🙂"},
    5: {"label": "ดีมาก",   "emoji": "👍"},
    6: {"label": "ดีที่สุด", "emoji": "🤩"},
}


@recordings_router.post("/{rec_id}/quality")
async def set_quality_rating(
    rec_id: str,
    req: QualityRatingRequest,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Set the user's verdict on how good a recording's transcription is.

    Accepts {rating: 1..6} where 1 = แย่มาก and 6 = ดีที่สุด. Storing the same
    rating twice is idempotent. Admin only: regular users can view existing
    ratings but cannot set them. Returns the normalized integer rating plus
    its Thai label.
    """
    from datetime import datetime, timezone

    rating = req.rating
    if not isinstance(rating, int) or rating not in _QUALITY_RATING_META:
        raise HTTPException(
            status_code=400,
            detail=f"rating must be an integer between 1 and 6 (got {rating!r})",
        )

    result = await db.execute(select(Recording).where(Recording.id == rec_id))
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    rec.quality_rating = rating
    rec.quality_rated_by_user_id = current_user.id
    rec.quality_rated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(
        f"Quality rating {rating} ({_QUALITY_RATING_META[rating]['label']}) "
        f"set on recording {rec_id} by {current_user.username}"
    )
    return {
        "success": True,
        "id": rec_id,
        "quality_rating": rating,
        "quality_rating_label": _QUALITY_RATING_META[rating]["label"],
        "quality_rated_by_user_id": current_user.id,
        "quality_rated_by_username": current_user.username,
    }


class TranscriptRequest(BaseModel):
    transcript: str


@recordings_router.put("/{rec_id}/transcript")
async def update_transcript(
    rec_id: str,
    req: TranscriptRequest,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Update transcript for a recording (after STT processing)."""
    result = await db.execute(select(Recording).where(Recording.id == rec_id))
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    rec.transcript = req.transcript
    await db.commit()
    return {"success": True, "id": rec_id}


# === STT (Speech-to-Text) endpoints ===

@recordings_router.post("/{rec_id}/transcribe")
async def transcribe_recording(
    rec_id: str,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Transcribe a single recording using the configured STT provider.

    Reads the audio file from disk, sends it to Speechmatics/Whisper,
    and stores the transcript in the DB.
    """
    result = await db.execute(select(Recording).where(Recording.id == rec_id))
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    if not rec.audio_file_path or not FSPath(rec.audio_file_path).exists():
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    # Mark as transcribing
    rec.status = RecordingStatus.TRANSCRIBING
    await db.commit()

    try:
        result = await transcribe(rec.audio_file_path)
        text = result.get("text", "").strip()
        lang = result.get("language", "th")
        provider = result.get("provider", "")
        error = result.get("error")
        stt_dur = result.get("duration")

        rec.transcript = text or "(ไม่สามารถถอดเสียงได้)"
        rec.transcript_language = lang
        rec.stt_provider = provider or None
        rec.stt_model = result.get("model") or None
        rec.stt_error = error or None
        rec.stt_duration = stt_dur
        rec.status = RecordingStatus.COMPLETED
        await db.commit()

        logger.info(f"Transcribed {rec_id}: {len(text)} chars (lang={lang}, provider={provider}, model={result.get('model')}, duration={stt_dur}s)")

        # Enqueue keyword-alert evaluation (non-blocking; worker handles it).
        try:
            from backend.alerts.jobs import enqueue_alert_job
            await enqueue_alert_job(rec_id)
        except Exception as e:
            logger.warning(f"Alert enqueue failed for {rec_id}: {e}")

        return {
            "success": True,
            "id": rec_id,
            "transcript": text,
            "language": lang,
            "provider": provider,
            "model": result.get("model"),
            "error": error,
            "stt_duration": stt_dur,
        }
    except Exception as e:
        logger.error(f"Transcription failed for {rec_id}: {e}", exc_info=True)
        rec.status = RecordingStatus.FAILED
        rec.stt_error = str(e)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")


class STTSettingsRequest(BaseModel):
    provider: Optional[str] = None  # "speechmatics" | "azure" | "whisper_local" | "groq"
    api_key: Optional[str] = None  # Speechmatics API key
    language: Optional[str] = None  # "th" | "en" | "auto"
    auto_transcribe: Optional[bool] = None
    # Speechmatics-specific options
    operating_point: Optional[str] = None  # "standard" | "enhanced"
    diarization: Optional[str] = None  # "none" | "speaker" | "channel"
    enable_entities: Optional[bool] = None
    # Azure-specific options
    azure_key: Optional[str] = None  # Azure Speech resource key
    azure_region: Optional[str] = None  # e.g. "southeastasia"
    # Groq Whisper API options
    groq_api_key: Optional[str] = None
    groq_model: Optional[str] = None  # e.g. "whisper-large-v3"
    # MaxPlus AI (api.maxplus-ai.cc) options
    maxplus_api_key: Optional[str] = None  # key format: ccsk-...
    maxplus_model: Optional[str] = None  # e.g. "whisper-large-v3" | "grok-stt"
    # Whisper Local (faster-whisper-server / Gradio API) options
    whisper_local_host: Optional[str] = None  # e.g. "192.168.1.201"
    whisper_local_port: Optional[int] = None  # e.g. 8100
    whisper_local_model: Optional[str] = None  # e.g. "Systran/faster-whisper-small"
    whisper_local_task: Optional[str] = None  # "transcribe" | "translate"
    whisper_local_temperature: Optional[float] = None  # 0.0–1.0
    # Per-provider audio preprocessing format (off|wav|flac|mp3).
    # "off" = send original WAV unchanged. "wav" = resample to 16kHz.
    # "flac"|"mp3" = also transcode (smaller upload, less bandwidth).
    speechmatics_audio_format: Optional[str] = None
    whisper_local_audio_format: Optional[str] = None
    azure_audio_format: Optional[str] = None
    groq_audio_format: Optional[str] = None
    maxplus_audio_format: Optional[str] = None


@settings_router.get("/stt")
async def get_stt_settings(current_user: User = Depends(require_admin)):
    """Get global STT settings (admin only; config affects all users)."""
    cfg = await _get_stt_config()
    # Effective MaxPlus key = DB value or env fallback (the transcription
    # service uses the same chain), so the UI hint reflects what will be used.
    mp_key = cfg.get("maxplus_api_key") or settings.MAXPLUS_API_KEY
    return {
        "provider": cfg["provider"],
        "api_key": mask_value(cfg["api_key"]) if cfg["api_key"] else "",
        "api_key_set": bool(cfg["api_key"]),
        "language": cfg["language"],
        "auto_transcribe": cfg["auto_transcribe"],
        "operating_point": cfg["operating_point"],
        "diarization": cfg["diarization"],
        "enable_entities": cfg["enable_entities"],
        "azure_key": mask_value(cfg.get("azure_key", "")) if cfg.get("azure_key") else "",
        "azure_key_set": bool(cfg.get("azure_key")),
        "azure_region": cfg.get("azure_region", "southeastasia"),
        "groq_api_key": mask_value(cfg.get("groq_api_key", "")) if cfg.get("groq_api_key") else "",
        "groq_api_key_set": bool(cfg.get("groq_api_key")),
        "groq_model": cfg.get("groq_model", "whisper-large-v3"),
        "maxplus_api_key": mask_value(mp_key) if mp_key else "",
        "maxplus_api_key_set": bool(mp_key),
        "maxplus_model": cfg.get("maxplus_model", "whisper-large-v3"),
        "whisper_local_host": cfg.get("whisper_local_host", ""),
        "whisper_local_port": cfg.get("whisper_local_port", 8100),
        "whisper_local_model": cfg.get("whisper_local_model", "Systran/faster-whisper-small"),
        "whisper_local_task": cfg.get("whisper_local_task", "transcribe"),
        "whisper_local_temperature": cfg.get("whisper_local_temperature", 0.0),
        "speechmatics_audio_format": cfg.get("speechmatics_audio_format", "off"),
        "whisper_local_audio_format": cfg.get("whisper_local_audio_format", "off"),
        "azure_audio_format": cfg.get("azure_audio_format", "off"),
        "groq_audio_format": cfg.get("groq_audio_format", "off"),
        "maxplus_audio_format": cfg.get("maxplus_audio_format", "off"),
    }


@settings_router.put("/stt")
async def update_stt_settings(
    req: STTSettingsRequest,
    current_user: User = Depends(require_admin),
):
    """Update global STT settings (admin only; config affects all users).

    Note: if `api_key`/`azure_key` is empty or a masked placeholder, the
    existing key is kept.

    Validation runs first; all accepted values are collected into one dict and
    written in a SINGLE transaction via set_settings_batch(). Previously this
    called set_setting() ~15 times, each opening its own session and committing
    (~15 × the per-commit fsync cost on the event loop).
    """
    updates: dict[str, str] = {}

    if req.provider is not None:
        if req.provider not in ("speechmatics", "azure", "whisper_local", "groq", "maxplus"):
            raise HTTPException(status_code=400, detail="Provider must be 'speechmatics', 'azure', 'whisper_local', 'groq', or 'maxplus'")
        updates["stt_provider"] = req.provider

    # Only update API key if a real (non-empty, non-masked) value is provided.
    # `is_masked_value()` also catches the "***" form returned by mask_value()
    # for short secrets — previously only "..." was rejected, so "***" would
    # overwrite the real key (root cause of Speechmatics key corruption).
    if req.api_key is not None and req.api_key.strip() and not is_masked_value(req.api_key):
        updates["stt_speechmatics_api_key"] = req.api_key.strip()

    if req.language is not None:
        if req.language not in ("th", "en", "auto"):
            raise HTTPException(status_code=400, detail="Language must be 'th', 'en', or 'auto'")
        updates["stt_language"] = req.language

    if req.auto_transcribe is not None:
        updates["stt_auto_transcribe"] = "true" if req.auto_transcribe else "false"

    # Speechmatics-specific options
    if req.operating_point is not None:
        if req.operating_point not in ("standard", "enhanced"):
            raise HTTPException(status_code=400, detail="operating_point must be 'standard' or 'enhanced'")
        updates["stt_operating_point"] = req.operating_point

    if req.diarization is not None:
        if req.diarization not in ("none", "speaker", "channel"):
            raise HTTPException(status_code=400, detail="diarization must be 'none', 'speaker', or 'channel'")
        updates["stt_diarization"] = req.diarization

    if req.enable_entities is not None:
        updates["stt_enable_entities"] = "true" if req.enable_entities else "false"

    # Azure-specific options
    if req.azure_key is not None and req.azure_key.strip() and not is_masked_value(req.azure_key):
        updates["stt_azure_key"] = req.azure_key.strip()

    if req.azure_region is not None and req.azure_region.strip():
        updates["stt_azure_region"] = req.azure_region.strip()

    # Groq-specific options
    if req.groq_api_key is not None and req.groq_api_key.strip() and not is_masked_value(req.groq_api_key):
        updates["stt_groq_api_key"] = req.groq_api_key.strip()
    if req.groq_model is not None and req.groq_model.strip():
        model = req.groq_model.strip()
        _VALID_GROQ_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en")
        if model not in _VALID_GROQ_MODELS:
            raise HTTPException(status_code=400, detail=f"groq_model must be one of {_VALID_GROQ_MODELS}")
        updates["stt_groq_model"] = model

    # MaxPlus-specific options. The model catalog is served live by the
    # provider's GET /v1/models endpoint, so we validate shape/length only —
    # a hard allowlist would reject legitimately new models.
    if req.maxplus_api_key is not None and req.maxplus_api_key.strip() and not is_masked_value(req.maxplus_api_key):
        mp_key = req.maxplus_api_key.strip()
        if not mp_key.startswith("ccsk-"):
            raise HTTPException(status_code=400, detail="maxplus_api_key must start with 'ccsk-'")
        updates["stt_maxplus_api_key"] = mp_key
    if req.maxplus_model is not None and req.maxplus_model.strip():
        mp_model = req.maxplus_model.strip()
        if len(mp_model) > 100:
            raise HTTPException(status_code=400, detail="maxplus_model too long")
        updates["stt_maxplus_model"] = mp_model

    # Whisper Local-specific options
    if req.whisper_local_host is not None:
        updates["stt_whisper_local_host"] = req.whisper_local_host.strip()
    if req.whisper_local_port is not None:
        updates["stt_whisper_local_port"] = str(int(req.whisper_local_port))
    if req.whisper_local_model is not None and req.whisper_local_model.strip():
        updates["stt_whisper_local_model"] = req.whisper_local_model.strip()
    if req.whisper_local_task is not None:
        if req.whisper_local_task not in ("transcribe", "translate"):
            raise HTTPException(status_code=400, detail="whisper_local_task must be 'transcribe' or 'translate'")
        updates["stt_whisper_local_task"] = req.whisper_local_task
    if req.whisper_local_temperature is not None:
        t = float(req.whisper_local_temperature)
        if t < 0.0 or t > 1.0:
            raise HTTPException(status_code=400, detail="whisper_local_temperature must be between 0.0 and 1.0")
        updates["stt_whisper_local_temperature"] = str(t)

    # Per-provider audio preprocessing format
    _VALID_AUDIO_FMT = ("off", "wav", "flac", "mp3")
    if req.speechmatics_audio_format is not None:
        v = req.speechmatics_audio_format.lower()
        if v not in _VALID_AUDIO_FMT:
            raise HTTPException(status_code=400, detail=f"audio_format must be one of {_VALID_AUDIO_FMT}")
        updates["stt_speechmatics_audio_format"] = v
    if req.whisper_local_audio_format is not None:
        v = req.whisper_local_audio_format.lower()
        if v not in _VALID_AUDIO_FMT:
            raise HTTPException(status_code=400, detail=f"audio_format must be one of {_VALID_AUDIO_FMT}")
        updates["stt_whisper_local_audio_format"] = v
    if req.azure_audio_format is not None:
        v = req.azure_audio_format.lower()
        # Azure Speech SDK only supports WAV; clamp mp3/flac -> wav automatically.
        if v in ("mp3", "flac"):
            v = "wav"
        if v not in _VALID_AUDIO_FMT:
            raise HTTPException(status_code=400, detail=f"audio_format must be one of {_VALID_AUDIO_FMT}")
        updates["stt_azure_audio_format"] = v
    if req.groq_audio_format is not None:
        v = req.groq_audio_format.lower()
        if v not in _VALID_AUDIO_FMT:
            raise HTTPException(status_code=400, detail=f"audio_format must be one of {_VALID_AUDIO_FMT}")
        updates["stt_groq_audio_format"] = v
    if req.maxplus_audio_format is not None:
        v = req.maxplus_audio_format.lower()
        if v not in _VALID_AUDIO_FMT:
            raise HTTPException(status_code=400, detail=f"audio_format must be one of {_VALID_AUDIO_FMT}")
        updates["stt_maxplus_audio_format"] = v

    # Write all accepted values in ONE transaction (single commit).
    await set_settings_batch(updates)

    # Return updated config
    cfg = await _get_stt_config()
    # Effective MaxPlus key = DB value or env fallback (same chain the
    # transcription service uses) so the UI summary reflects reality.
    mp_key = cfg.get("maxplus_api_key") or settings.MAXPLUS_API_KEY
    return {
        "success": True,
        "provider": cfg["provider"],
        "api_key": mask_value(cfg["api_key"]) if cfg["api_key"] else "",
        "api_key_set": bool(cfg["api_key"]),
        "language": cfg["language"],
        "auto_transcribe": cfg["auto_transcribe"],
        "operating_point": cfg["operating_point"],
        "diarization": cfg["diarization"],
        "enable_entities": cfg["enable_entities"],
        "azure_key": mask_value(cfg.get("azure_key", "")) if cfg.get("azure_key") else "",
        "azure_key_set": bool(cfg.get("azure_key")),
        "azure_region": cfg.get("azure_region", "southeastasia"),
        "groq_api_key": mask_value(cfg.get("groq_api_key", "")) if cfg.get("groq_api_key") else "",
        "groq_api_key_set": bool(cfg.get("groq_api_key")),
        "groq_model": cfg.get("groq_model", "whisper-large-v3"),
        "maxplus_api_key": mask_value(mp_key) if mp_key else "",
        "maxplus_api_key_set": bool(mp_key),
        "maxplus_model": cfg.get("maxplus_model", "whisper-large-v3"),
        "whisper_local_host": cfg.get("whisper_local_host", ""),
        "whisper_local_port": cfg.get("whisper_local_port", 8100),
        "whisper_local_model": cfg.get("whisper_local_model", "Systran/faster-whisper-small"),
        "whisper_local_task": cfg.get("whisper_local_task", "transcribe"),
        "whisper_local_temperature": cfg.get("whisper_local_temperature", 0.0),
        "speechmatics_audio_format": cfg.get("speechmatics_audio_format", "off"),
        "whisper_local_audio_format": cfg.get("whisper_local_audio_format", "off"),
        "azure_audio_format": cfg.get("azure_audio_format", "off"),
        "groq_audio_format": cfg.get("groq_audio_format", "off"),
        "maxplus_audio_format": cfg.get("maxplus_audio_format", "off"),
    }


@settings_router.get("/stt/maxplus/models")
async def get_maxplus_models(
    key: Optional[str] = None,
    current_user: User = Depends(require_admin),
):
    """List MaxPlus AI transcription models available to the configured key.

    Proxies the provider's OpenAI-compatible ``GET /v1/models`` endpoint so
    the Settings UI can populate the model dropdown with the live catalog.
    ``key`` (optional) lets the UI preview models for a not-yet-saved API
    key; otherwise the saved DB value / env fallback is used.

    Returns 200 with {"success": false, "error": ...} on upstream failure so
    the UI can degrade gracefully to its static seed options.
    """
    # Prefer an explicitly provided (non-masked) key, e.g. typed into the
    # form but not saved yet; otherwise use the stored configuration.
    api_key = None
    if key and key.strip() and not is_masked_value(key):
        api_key = key.strip()
    else:
        cfg = await _get_stt_config()
        api_key = cfg.get("maxplus_api_key") or settings.MAXPLUS_API_KEY

    result = await list_maxplus_models(api_key)
    return {
        "success": result["error"] is None,
        "models": result["models"],
        "error": result["error"],
    }


# === SDR Device management endpoints (registry + active device) ===
# All endpoints require admin because device config affects the global SDR
# connection used by everyone.

@devices_router.get("/types")
async def list_device_types(current_user: User = Depends(require_admin)):
    """List supported device/connection types and their config field schemas.

    The frontend wizard uses this to render the correct configuration form
    for the selected device type.
    """
    # Convert dict → array of {value, label, description} for the frontend wizard
    types_list = [
        {"value": key, "label": val.get("label", key), "description": val.get("description", "")}
        for key, val in DEVICE_TYPES.items()
    ]
    return {"types": types_list}


@devices_router.get("")
async def list_devices(
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """List all configured SDR devices (registry)."""
    result = await db.execute(select(SdrDevice).order_by(SdrDevice.created_at.asc()))
    devices = result.scalars().all()
    return {"items": [d.to_dict() for d in devices]}


class DeviceCreateRequest(BaseModel):
    name: str
    type: str = "rtl_tcp"
    config: Optional[dict] = None
    activate: Optional[bool] = False  # set as active immediately


@devices_router.post("")
async def create_device(
    req: DeviceCreateRequest,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Create a new SDR device (registry entry).

    If `activate` is true (or no active device exists), the new device is set
    as the active one. Pass `config` with the type-specific fields
    (e.g. host/port/frequency for rtl_tcp). If omitted, type defaults are used.
    """
    if req.type not in DEVICE_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported device type: {req.type}")
    config = req.config if req.config is not None else default_config_for_type(req.type)

    # Determine if this should become active
    result = await db.execute(select(SdrDevice).where(SdrDevice.is_active == True))  # noqa: E712
    existing_active = result.scalar_one_or_none()
    make_active = bool(req.activate) or (existing_active is None)

    device = SdrDevice(name=req.name.strip(), type=req.type, is_active=False)
    device.set_config(config)
    db.add(device)

    if make_active:
        # Deactivate all others, then activate the new one
        all_result = await db.execute(select(SdrDevice))
        for d in all_result.scalars().all():
            d.is_active = False
        device.is_active = True

    await db.commit()
    await db.refresh(device)
    device_dict = device.to_dict()

    # Apply to the live manager if it's now the active device
    if make_active:
        try:
            await sdr_manager.activate_device(device.id, reconnect=False)
        except Exception as e:
            logger.warning(f"Failed to apply new active device config: {e}")

    return device_dict


@devices_router.get("/{device_id}")
async def get_device(
    device_id: str,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Get a single SDR device by ID."""
    device = await db.get(SdrDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device.to_dict()


class DeviceUpdateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    config: Optional[dict] = None


@devices_router.put("/{device_id}")
async def update_device(
    device_id: str,
    req: DeviceUpdateRequest,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Update an existing SDR device's name/type/config."""
    device = await db.get(SdrDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if req.type is not None and req.type not in DEVICE_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported device type: {req.type}")
    if req.name is not None:
        device.name = req.name.strip()
    if req.type is not None:
        device.type = req.type
    if req.config is not None:
        device.set_config(req.config)
    await db.commit()
    await db.refresh(device)
    device_dict = device.to_dict()

    # If this is the active device, apply the new config live
    if device.is_active:
        try:
            await sdr_manager.activate_device(device.id, reconnect=False)
        except Exception as e:
            logger.warning(f"Failed to apply updated active device config: {e}")

    return device_dict


@devices_router.delete("/{device_id}")
async def delete_device(
    device_id: str,
    current_user: User = Depends(require_admin),
    db = Depends(get_db),
):
    """Delete an SDR device.

    If the deleted device was active, another device (if any) is activated in
    its place so there is always an active device when the registry is
    non-empty. Deleting the last device leaves no active device (the legacy
    sdr_settings.json values continue to be used).
    """
    device = await db.get(SdrDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Null out FK references from recordings and recording_jobs before deleting.
    # SQLite's FK enforcement may or may not be active, so we handle it manually
    # to avoid "FOREIGN KEY constraint failed" (500 Internal Server Error).
    from backend.models.recording import Recording
    from backend.models.recording_job import RecordingJob

    recs = (await db.execute(select(Recording).where(Recording.device_id == device_id))).scalars().all()
    for rec in recs:
        rec.device_id = None
    # recording_jobs.device_id is now nullable — null it out so the job
    # survives (with no device) and the UI can show a red-border warning.
    # BUT first stop any active DeviceCapture for those jobs to prevent
    # the scheduler from trying to use a deleted device.
    from backend.sdr.job_scheduler import job_scheduler
    jobs = (await db.execute(select(RecordingJob).where(RecordingJob.device_id == device_id))).scalars().all()
    for job in jobs:
        # Stop the capture if it's currently running
        cap = job_scheduler.get_capture(job.id)
        if cap:
            try:
                await cap.stop()
            except Exception as e:
                logger.warning(f"Error stopping capture for job {job.id} during device delete: {e}")
        job.device_id = None

    was_active = bool(device.is_active)
    await db.delete(device)
    await db.commit()

    # If we removed the active device, promote the oldest remaining one
    if was_active:
        result = await db.execute(select(SdrDevice).order_by(SdrDevice.created_at.asc()))
        remaining = result.scalars().all()
        if remaining:
            remaining[0].is_active = True
            await db.commit()
            try:
                await sdr_manager.activate_device(remaining[0].id, reconnect=False)
            except Exception as e:
                logger.warning(f"Failed to promote fallback device: {e}")

    return {"success": True, "id": device_id}


@devices_router.post("/{device_id}/activate")
async def activate_device(
    device_id: str,
    current_user: User = Depends(require_admin),
):
    """Set a device as the active one and apply its config to the live SDR.

    If the SDR is currently connected/streaming, it reconnects so the new
    device takes effect immediately.
    """
    try:
        return await sdr_manager.activate_device(device_id, reconnect=True)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))