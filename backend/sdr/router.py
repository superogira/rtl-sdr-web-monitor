"""
SDR API Routes - control RTL-TCP, streaming, recording.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from pathlib import Path as FSPath
import os

from backend.auth.dependencies import get_current_user
from backend.models.user import User
from backend.sdr.manager import sdr_manager
from backend.audio.demod import DemodMode
from backend.database import get_db
from backend.models.recording import Recording
from backend.config import settings
from sqlalchemy import select, func

router = APIRouter(prefix="/api/sdr", tags=["SDR Control"])

# Separate router for recordings (different prefix)
recordings_router = APIRouter(prefix="/api/recordings", tags=["Recordings"])


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


# === Endpoints ===

@router.get("/status")
async def get_sdr_status(current_user: User = Depends(get_current_user)):
    """Get current SDR status."""
    return sdr_manager.status


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
    current_user: User = Depends(get_current_user),
):
    """Connect to RTL-TCP server.

    Optionally accepts {host, port} to update the target before connecting.
    """
    if req is not None and req.host:
        # If currently connected, disconnect first so new endpoint applies.
        if sdr_manager.client.connected:
            await sdr_manager.disconnect()
        sdr_manager.set_connection(req.host, req.port)
    result = await sdr_manager.connect()
    if not result["success"]:
        raise HTTPException(status_code=503, detail="Failed to connect to RTL-TCP")
    return result


@router.post("/disconnect")
async def disconnect_sdr(current_user: User = Depends(get_current_user)):
    """Disconnect from RTL-TCP."""
    await sdr_manager.disconnect()
    return {"success": True}


@router.post("/frequency")
async def set_frequency(
    req: FrequencyRequest,
    current_user: User = Depends(get_current_user),
):
    """Set center frequency."""
    if not sdr_manager.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    await sdr_manager.set_frequency(req.frequency)
    return {"frequency": req.frequency}


@router.post("/sample-rate")
async def set_sample_rate(
    req: SampleRateRequest,
    current_user: User = Depends(get_current_user),
):
    """Set sample rate."""
    if not sdr_manager.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    await sdr_manager.set_sample_rate(req.sample_rate)
    return {"sample_rate": req.sample_rate}


@router.post("/gain")
async def set_gain(
    req: GainRequest,
    current_user: User = Depends(get_current_user),
):
    """Set gain."""
    if not sdr_manager.client.connected:
        raise HTTPException(status_code=400, detail="Not connected")
    await sdr_manager.set_gain(req.gain)
    return {"gain": req.gain}


@router.post("/mode")
async def set_mode(
    req: ModeRequest,
    current_user: User = Depends(get_current_user),
):
    """Set demodulation mode."""
    try:
        await sdr_manager.set_mode(req.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {req.mode}. Use FM, AM, USB, LSB")
    return {"mode": req.mode.upper()}


@router.post("/squelch")
async def set_squelch(
    req: SquelchRequest,
    current_user: User = Depends(get_current_user),
):
    """Set squelch level."""
    sdr_manager.set_squelch(req.level)
    return {"squelch": req.level}


@router.post("/fft-size")
async def set_fft_size(
    req: FFTSizeRequest,
    current_user: User = Depends(get_current_user),
):
    """Set FFT output bins (waterfall resolution)."""
    try:
        sdr_manager.set_fft_bins(req.fft_size)
        return {"fft_size": req.fft_size}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bandwidth")
async def set_bandwidth(
    req: BandwidthRequest,
    current_user: User = Depends(get_current_user),
):
    """Set channel bandwidth (Hz) for signal level computation."""
    try:
        sdr_manager.set_channel_bw(req.bandwidth)
        return {"bandwidth": req.bandwidth}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/freq-correction")
async def set_freq_correction(
    req: FreqCorrectionRequest,
    current_user: User = Depends(get_current_user),
):
    """Set frequency correction in PPM (parts per million)."""
    await sdr_manager.set_freq_correction(req.ppm)
    return {"ppm": req.ppm}


@router.post("/stream/start")
async def start_streaming(current_user: User = Depends(get_current_user)):
    """Start streaming IQ data."""
    result = await sdr_manager.start_streaming()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/stream/stop")
async def stop_streaming(current_user: User = Depends(get_current_user)):
    """Stop streaming."""
    return await sdr_manager.stop_streaming()


@router.post("/record/start")
async def start_recording(current_user: User = Depends(get_current_user)):
    """Start recording manually."""
    result = await sdr_manager.start_recording()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/record/stop")
async def stop_recording(current_user: User = Depends(get_current_user)):
    """Stop recording."""
    result = await sdr_manager.stop_recording()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# === Recordings endpoints (from DB) ===

@recordings_router.get("")
async def list_recordings(
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """List recordings from database (paginated, newest first)."""
    query = select(Recording).order_by(Recording.recorded_at.desc())
    if source:
        query = query.where(Recording.source == source)
    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    recordings = result.scalars().all()

    # Count total
    count_q = select(func.count(Recording.id))
    if source:
        count_q = count_q.where(Recording.source == source)
    total_result = await db.execute(count_q)
    total = total_result.scalar()

    return {
        "items": [r.to_dict() for r in recordings],
        "total": total,
        "limit": limit,
        "offset": offset,
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
    current_user: User = Depends(get_current_user),
    db = Depends(get_db),
):
    """Delete a recording (DB row + audio file)."""
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
