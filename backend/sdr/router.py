"""
SDR API Routes - control RTL-TCP, streaming, recording.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from backend.auth.dependencies import get_current_user
from backend.models.user import User
from backend.sdr.manager import sdr_manager
from backend.audio.demod import DemodMode

router = APIRouter(prefix="/api/sdr", tags=["SDR Control"])


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