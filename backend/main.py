import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.config import settings
from backend.database import init_db
from backend.auth.router import router as auth_router
from backend.sdr.router import router as sdr_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    # Startup
    print(f"🚀 {settings.APP_NAME} starting...")
    await init_db()
    print("✅ Database initialized")

    # Ensure recordings directory exists
    recordings_dir = Path(settings.RECORDINGS_DIR)
    recordings_dir.mkdir(parents=True, exist_ok=True)

    print(f"📡 RTL-TCP: {settings.RTL_TCP_HOST}:{settings.RTL_TCP_PORT}")
    print(f"🌐 Server: http://{settings.SERVER_HOST}:{settings.SERVER_PORT}")
    print(f"🔧 Environment: {settings.APP_ENV}")

    yield

    # Shutdown
    print("👋 Shutting down...")


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    description="RTL-SDR Web Monitor - Radio signal monitoring, recording, and transcription",
    version="0.1.0",
    lifespan=lifespan,
)

# Frontend is served from same origin, so not needed in most cases
# Only add for production with separate frontend server

# === API Routes ===
app.include_router(auth_router)
app.include_router(sdr_router)


# === Health Check ===
@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "app_name": settings.APP_NAME,
        "environment": settings.APP_ENV,
    }


# === WebSocket endpoint ===
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Main WebSocket endpoint for real-time updates."""
    import asyncio
    import json
    import logging
    from backend.sdr.manager import sdr_manager

    logger_ws = logging.getLogger("websocket")

    try:
        await websocket.accept()
        logger_ws.info("🔌 WebSocket client connected")
    except Exception as e:
        logger_ws.error(f"WebSocket accept failed: {e}")
        return

    # Register spectrum callback
    spectrum_data = {"latest": None}
    
    def on_spectrum(data):
        spectrum_data["latest"] = data

    sdr_manager.on_spectrum(on_spectrum)

    import base64
    import numpy as np

    status_counter = 0
    try:
        while True:
            # Send spectrum data at ~30 FPS (binary base64 for efficiency)
            if spectrum_data["latest"]:
                # Encode as int16 binary (1024 bins × 2 bytes = 2KB → ~2.7KB base64)
                arr = np.array(spectrum_data["latest"], dtype=np.int16)
                b64 = base64.b64encode(arr.tobytes()).decode("ascii")
                await websocket.send_text(json.dumps({
                    "type": "spectrum",
                    "data": b64
                }))
                spectrum_data["latest"] = None

            # Send status at ~1 FPS (every 30th iteration)
            status_counter += 1
            if status_counter >= 30:
                status_counter = 0
                status = sdr_manager.status
                await websocket.send_text(json.dumps({"type": "status", "data": status}))

                # Check for new recordings
                new = sdr_manager.get_new_recordings()
                for rec in new:
                    await websocket.send_text(json.dumps({"type": "new_recording", "data": rec}))

            await asyncio.sleep(0.033)  # ~30 FPS
    except Exception as e:
        logger_ws.info(f"🔌 WebSocket disconnected: {e}")


# === Audio WebSocket endpoint (live audio streaming) ===
@app.websocket("/ws/audio")
async def audio_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for live audio streaming (Phase A: PCM 16-bit).

    Protocol:
      - Server → Client: binary frames "AUD1" + sample_rate + num_samples + PCM data
      - Client → Server: optional text "ping" → server responds "pong" (keepalive)
      - Server → Client: text {"type":"ready", ...} on connect (diagnostic)

    Concurrency: This endpoint owns the only task that calls send_bytes() on this
    WebSocket. It races queue.get() against receive_text() using asyncio.wait,
    so neither audio frames nor pings block each other.
    """
    import asyncio
    import json
    import logging
    from fastapi import WebSocketDisconnect
    from backend.audio.broadcaster import audio_broadcaster
    from backend.sdr.manager import sdr_manager

    logger_aud = logging.getLogger("audio_ws")

    try:
        await websocket.accept()
    except Exception as e:
        logger_aud.error(f"🔊 Audio WS accept failed: {e}")
        return

    queue = await audio_broadcaster.subscribe()
    logger_aud.info(
        f"🔊 Audio WS connected (listeners: {audio_broadcaster.listener_count}, "
        f"streaming: {sdr_manager.is_streaming}, rtl: {sdr_manager.client.connected})"
    )

    # Send a diagnostic text frame immediately so the client can log backend state
    try:
        await websocket.send_text(json.dumps({
            "type": "ready",
            "streaming": sdr_manager.is_streaming,
            "rtl_connected": sdr_manager.client.connected,
            "listeners": audio_broadcaster.listener_count,
        }))
    except Exception:
        pass

    try:
        while True:
            recv_task = asyncio.ensure_future(websocket.receive_text())
            frame_task = asyncio.ensure_future(queue.get())
            done, pending = await asyncio.wait(
                {recv_task, frame_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            if recv_task in done:
                try:
                    msg = recv_task.result()
                    if msg == "ping":
                        await websocket.send_text("pong")
                    elif msg == "close":
                        break
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger_aud.info(f"🔊 Audio WS recv error: {e}")
                    break

            if frame_task in done:
                try:
                    frame = frame_task.result()
                    await websocket.send_bytes(frame)
                except Exception as e:
                    logger_aud.info(f"🔊 Audio WS send_bytes error: {e}")
                    break
    except WebSocketDisconnect:
        logger_aud.info("🔊 Audio WS client disconnected")
    except Exception as e:
        logger_aud.warning(f"🔊 Audio WS error: {e}")
    finally:
        await audio_broadcaster.unsubscribe(queue)
        logger_aud.info(
            f"🔊 Audio WS disconnected (listeners: {audio_broadcaster.listener_count})"
        )


# === Audio Stats endpoint ===
@app.get("/api/audio/stats")
async def audio_stats():
    """Get audio broadcaster statistics."""
    from backend.audio.broadcaster import audio_broadcaster
    return audio_broadcaster.get_stats()


@app.get("/api/audio/diag")
async def audio_diag():
    """Diagnostic endpoint for audio pipeline troubleshooting.

    Returns the full state needed to understand why audio may not be flowing.
    """
    from backend.audio.broadcaster import audio_broadcaster
    from backend.sdr.manager import sdr_manager
    stats = audio_broadcaster.get_stats()
    return {
        **stats,
        "rtl_connected": sdr_manager.client.connected,
        "streaming": sdr_manager.is_streaming,
        "frequency": sdr_manager.client._frequency,
        "sample_rate": sdr_manager.client._sample_rate,
        "mode": sdr_manager.mode.value,
        "audio_rate": sdr_manager.audio_rate,
        "signal_level_db": round(sdr_manager.signal_level, 2),
    }


# === Serve Frontend Static Files ===
frontend_dir = Path(__file__).parent.parent / "frontend"

# Mount static assets
if (frontend_dir / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dir / "assets")), name="assets")

if (frontend_dir / "css").exists():
    app.mount("/css", StaticFiles(directory=str(frontend_dir / "css")), name="css")

if (frontend_dir / "js").exists():
    app.mount("/js", StaticFiles(directory=str(frontend_dir / "js")), name="js")


# Serve recordings directory
recordings_dir = Path(settings.RECORDINGS_DIR)
recordings_dir.mkdir(parents=True, exist_ok=True)
if recordings_dir.exists():
    app.mount("/recordings", StaticFiles(directory=str(recordings_dir)), name="recordings")


# === Serve Frontend HTML Pages ===
@app.get("/")
async def serve_index():
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/login")
async def serve_login():
    return FileResponse(str(frontend_dir / "pages" / "login.html"))


@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse(str(frontend_dir / "pages" / "dashboard.html"))

@app.get("/waterfall-test")
async def serve_waterfall_test():
    return FileResponse(str(frontend_dir / "pages" / "waterfall-test.html"))


@app.get("/recordings")
async def serve_recordings():
    return FileResponse(str(frontend_dir / "pages" / "recordings.html"))


@app.get("/settings")
async def serve_settings():
    return FileResponse(str(frontend_dir / "pages" / "settings.html"))


@app.get("/admin/users")
async def serve_admin_users():
    return FileResponse(str(frontend_dir / "pages" / "admin" / "users.html"))