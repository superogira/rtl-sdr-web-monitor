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
from backend.sdr.router import (
    router as sdr_router,
    recordings_router,
    settings_router,
    devices_router,
)
from backend.sdr.recording_jobs_router import recording_jobs_router


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

    # Load configurable VFO passband margin from app settings
    from backend.sdr.manager import sdr_manager
    await sdr_manager.load_vfo_margin_ratio()
    # Load configurable max concurrent listeners from app settings
    await sdr_manager.load_max_listeners()
    # Load the active SDR device's config from the registry (overrides the
    # legacy sdr_settings.json connection values if an active device exists).
    await sdr_manager.load_active_device()

    # Start the background recording job scheduler (multi-device capture).
    # Runs independently from the interactive SDRManager — it spawns its own
    # RtlTcpClient per active recording job.
    from backend.sdr.job_scheduler import job_scheduler
    try:
        await job_scheduler.start()
    except Exception as e:
        print(f"⚠️ JobScheduler start failed (will retry on next tick): {e}")

    # Auto-start streaming in background (24/7 operation)
    if settings.AUTO_START_STREAMING:
        import asyncio
        from backend.sdr.manager import sdr_manager

        async def _autostart_loop():
            retries = 0
            max_retries = settings.AUTO_START_MAX_RETRIES
            interval = settings.AUTO_START_RETRY_INTERVAL
            while True:
                if max_retries == 0 or retries < max_retries:
                    try:
                        print(f"🔄 [autostart] Connecting to RTL-TCP "
                              f"{settings.RTL_TCP_HOST}:{settings.RTL_TCP_PORT} "
                              f"(attempt {retries + 1}" +
                              (f"/{max_retries}" if max_retries > 0 else "") + ")")
                        connect_result = await sdr_manager.connect()
                        if connect_result.get("success"):
                            print("✅ [autostart] Connected to RTL-TCP")
                            start_result = await sdr_manager.start_streaming()
                            if "error" not in start_result:
                                print("✅ [autostart] Streaming started (background 24/7)")
                                if sdr_manager._auto_recording:
                                    print("🎙️ [autostart] Auto-record active")
                                return  # Success — exit loop
                            else:
                                print(f"⚠️ [autostart] Streaming failed: {start_result['error']}")
                                await sdr_manager.disconnect()
                        else:
                            print(f"⚠️ [autostart] Connect failed: {connect_result}")
                    except Exception as e:
                        print(f"⚠️ [autostart] Error: {e}")
                    retries += 1
                await asyncio.sleep(interval)

        # Run autostart in background task
        asyncio.create_task(_autostart_loop())

    yield

    # Shutdown
    print("👋 Shutting down...")
    from backend.sdr.job_scheduler import job_scheduler
    try:
        await job_scheduler.stop()
    except Exception as e:
        print(f"⚠️ JobScheduler stop error: {e}")


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
app.include_router(recordings_router)
app.include_router(settings_router)
app.include_router(devices_router)
app.include_router(recording_jobs_router)


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

    # Register spectrum callback (per-connection; unregistered on disconnect
    # so a closed socket doesn't accumulate dead callbacks forever).
    spectrum_data = {"latest": None}

    def on_spectrum(data):
        spectrum_data["latest"] = data

    sdr_manager.on_spectrum(on_spectrum)

    # New-recording fan-out: each WS connection gets its OWN queue (unlike the
    # old get_new_recordings() poll, which cleared a single global list after
    # the FIRST reader — so with 2+ dashboards open only one of them ever saw
    # "รายการล่าสุดไม่อัพเดท"). The manager fires these callbacks on save.
    new_recording_queue: "asyncio.Queue" = asyncio.Queue(maxsize=64)

    async def on_new_recording(rec):
        try:
            new_recording_queue.put_nowait(rec)
        except asyncio.QueueFull:
            # Drop oldest so a slow client stays near current.
            try:
                new_recording_queue.get_nowait()
            except Exception:
                pass
            try:
                new_recording_queue.put_nowait(rec)
            except Exception:
                pass

    sdr_manager.on_new_recording(on_new_recording)

    status_counter = 0
    try:
        while True:
            # Send spectrum data as a BINARY WebSocket frame.
            # Frame format: 2-byte type tag (b"\x01\x00" = spectrum) + int16 LE
            # payload. This replaces the old base64-in-JSON scheme, removing
            # the ~33% base64 expansion and the per-byte atob() loop on the
            # client. Binary frames and text (JSON) frames can coexist on one
            # WebSocket connection; the client distinguishes them by type.
            if spectrum_data["latest"]:
                payload = spectrum_data["latest"]
                await websocket.send_bytes(b"\x01\x00" + payload)
                spectrum_data["latest"] = None

            # Drain any new-recording notifications for THIS client (fan-out,
            # so every open dashboard sees them, not just the first poller).
            while not new_recording_queue.empty():
                try:
                    rec = new_recording_queue.get_nowait()
                    await websocket.send_text(json.dumps({"type": "new_recording", "data": rec}))
                except asyncio.QueueEmpty:
                    break

            # Send status at ~1 FPS (every 30th iteration)
            status_counter += 1
            if status_counter >= 30:
                status_counter = 0
                status = sdr_manager.status
                await websocket.send_text(json.dumps({"type": "status", "data": status}))

            await asyncio.sleep(0.033)  # ~30 FPS
    except Exception as e:
        logger_ws.info(f"🔌 WebSocket disconnected: {e}")
    finally:
        # Always unregister so callbacks don't leak across reconnects.
        sdr_manager.off_spectrum(on_spectrum)
        sdr_manager.off_new_recording(on_new_recording)


# === Audio WebSocket endpoint (live audio streaming) ===
@app.websocket("/ws/audio")
async def audio_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for live, PER-LISTENER audio streaming (PCM 16-bit).

    OpenWebRX-style multi-listener model: each connection owns its own
    demodulator (in AudioBroadcaster.Listener), so it can listen to any
    frequency / mode / bandwidth inside the captured passband independently
    of every other user. One client tuning no longer moves anyone else.

    Protocol:
      - Server → Client: binary frames "AUD1" + sample_rate + num_samples + PCM data
      - Server → Client: text {"type":"ready", ...} on connect (diagnostic)
      - Client → Server (control, JSON text):
          {"type":"vfo","frequency":<Hz>}      set THIS listener's freq (absolute)
          {"type":"mode","mode":"FM|AM|USB|LSB"}
          {"type":"bandwidth","bandwidth":<Hz>}
        - Client → Server: text "ping" → server responds "pong" (keepalive)

    Concurrency: This endpoint owns the only task that calls send_bytes() on
    this WebSocket. It races listener.queue.get() against receive_text() using
    asyncio.wait, so neither audio frames nor control messages block.
    """
    import asyncio
    import json
    import logging
    from fastapi import WebSocketDisconnect
    from backend.audio.broadcaster import audio_broadcaster
    from backend.sdr.manager import sdr_manager
    from backend.auth.jwt import decode_token
    from backend.services.app_settings import get_setting

    logger_aud = logging.getLogger("audio_ws")

    # === Authenticate the WS connection via query param ?token=JWT ===
    # We can't use HTTP headers on WebSocket handshake from browsers, so the
    # client passes the access token as a query parameter. If valid, we attach
    # user_id + role to the listener so per-user manual recordings can be owned.
    token = websocket.query_params.get("token")
    ws_user_id = None
    ws_user_role = None
    if token:
        try:
            payload = decode_token(token)
            if payload and payload.get("type") == "access":
                ws_user_id = payload.get("sub")
                # Role may be in token; otherwise we trust the listener's later actions
                ws_user_role = payload.get("role")
        except Exception as e:
            logger_aud.debug(f"🔊 Audio WS token decode failed (anonymous): {e}")

    try:
        await websocket.accept()
    except Exception as e:
        logger_aud.error(f"🔊 Audio WS accept failed: {e}")
        return

    # Register a NEW per-listener demodulator. Initialize its VFO/mode/bw to
    # the SDR's current defaults so a freshly-connected user hears the same
    # channel as the global/default demodulator until they tune elsewhere.
    try:
        listener = await audio_broadcaster.subscribe(
            sample_rate=sdr_manager.client._sample_rate,
            audio_rate=sdr_manager.audio_rate,
            mode=sdr_manager.mode,
            channel_bw=sdr_manager._channel_bw,
            vfo_offset_hz=sdr_manager._vfo_offset_hz,
            user_id=ws_user_id,
            user_role=ws_user_role,
        )
        # Initialize the listener's squelch from the global squelch value so a
        # newly-connected user immediately hears the same squelch behavior as
        # the global setting (mute to silence when the signal is below the
        # threshold, like a real radio).
        listener.set_squelch(float(sdr_manager._squelch_level))
    except RuntimeError as e:
        # Listener cap reached
        await websocket.close(code=1013, reason=str(e))
        return

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
            "vfo_offset_hz": listener.vfo_offset_hz,
            "center_frequency": sdr_manager.client._frequency,
            "mode": listener.mode.value,
            "channel_bw": listener.channel_bw,
        }))
    except Exception:
        pass

    async def _handle_control(raw: str) -> bool:
        """Apply a client control message to THIS listener.

        Returns True if the connection should continue, False to close.
        """
        if raw == "ping":
            await websocket.send_text("pong")
            return True
        if raw == "close":
            return False
        try:
            msg = json.loads(raw)
        except Exception:
            return True
        mtype = msg.get("type")
        if mtype == "vfo":
            try:
                freq = int(msg["frequency"])
                offset = float(freq) - float(sdr_manager.client._frequency)
                max_off = sdr_manager.client._sample_rate * sdr_manager._vfo_margin_ratio
                offset = max(-max_off, min(max_off, offset))
                listener.set_vfo_offset(offset)
                await websocket.send_text(json.dumps({
                    "type": "vfo_ack",
                    "frequency": int(round(sdr_manager.client._frequency + offset)),
                    "vfo_offset_hz": offset,
                }))
                logger_aud.info(
                    f"🎯 Listener {listener.id[:8]} VFO → "
                    f"{(sdr_manager.client._frequency + offset)/1e6:.4f} MHz"
                )
            except (KeyError, ValueError, TypeError) as e:
                logger_aud.warning(f"🔊 Bad vfo control: {e}")
        elif mtype == "mode":
            try:
                listener.set_mode(str(msg["mode"]).upper())
            except Exception as e:
                logger_aud.warning(f"🔊 Bad mode control: {e}")
        elif mtype == "bandwidth":
            try:
                listener.set_bandwidth(int(msg["bandwidth"]))
            except Exception as e:
                logger_aud.warning(f"🔊 Bad bandwidth control: {e}")
        elif mtype == "squelch":
            # Per-listener squelch control: mute the live audio to silence
            # when the channel power drops below the given threshold (dB),
            # like a real radio. Values <= -120 disable squelch (pass all
            # audio including noise floor).
            try:
                listener.set_squelch(float(msg["level"]))
                await websocket.send_text(json.dumps({
                    "type": "squelch_ack",
                    "level": listener.squelch_db,
                }))
                logger_aud.info(
                    f"🔊 Listener {listener.id[:8]} squelch → "
                    f"{listener.squelch_db:.1f} dB"
                )
            except Exception as e:
                logger_aud.warning(f"🔊 Bad squelch control: {e}")
        elif mtype == "auto_squelch":
            # Per-listener AUTO squelch toggle: when on, the threshold tracks
            # the rolling noise floor adaptively (no need to hand-tune the SQL
            # slider as the band gets busy/quiet). When off, the manual
            # squelch_db is used again.
            try:
                enabled = bool(msg.get("enabled", False))
                listener.set_auto_squelch(enabled)
                await websocket.send_text(json.dumps({
                    "type": "auto_squelch_ack",
                    "enabled": listener.auto_squelch,
                }))
                logger_aud.info(
                    f"🔊 Listener {listener.id[:8]} auto-squelch → "
                    f"{'ON' if listener.auto_squelch else 'OFF'}"
                )
            except Exception as e:
                logger_aud.warning(f"🔊 Bad auto_squelch control: {e}")
        elif mtype == "record_start":
            # Per-user manual recording: check if the system allows it.
            # Admins can always record; regular users are subject to the
            # "manual_recording_enabled" app setting (admin toggle).
            if ws_user_role != "admin":
                enabled = await get_setting("manual_recording_enabled", "true")
                if str(enabled).lower() != "true":
                    await websocket.send_text(json.dumps({
                        "type": "record_ack",
                        "status": "disabled",
                        "message": "ระบบบันทึกถูกปิดไว้ชั่วคราว กรุณาติดต่อผู้ดูแลระบบ",
                    }))
                    return True
            started = audio_broadcaster.start_listener_recording(listener)
            await websocket.send_text(json.dumps({
                "type": "record_ack",
                "status": "recording" if started else "already_recording",
            }))
        elif mtype == "record_stop":
            rec_data = audio_broadcaster.stop_listener_recording(listener)
            if rec_data is None:
                await websocket.send_text(json.dumps({
                    "type": "record_ack",
                    "status": "no_audio",
                }))
                return True
            # Save to disk + DB asynchronously (don't block the audio loop)
            saved = await sdr_manager.save_listener_recording(rec_data)
            await websocket.send_text(json.dumps({
                "type": "record_ack",
                "status": "saved",
                "recording": saved,
            }))
        elif mtype == "auto_record":
            # Per-listener AUTO-record toggle (multi-user). Each user can
            # independently auto-record the channel they are listening to,
            # gated by their OWN squelch threshold + measured signal. Admins
            # bypass the manual_recording_enabled gate (same as manual record).
            try:
                enabled = bool(msg.get("enabled", False))
                if ws_user_role != "admin":
                    gate = await get_setting("manual_recording_enabled", "true")
                    if str(gate).lower() != "true":
                        await websocket.send_text(json.dumps({
                            "type": "auto_record_ack",
                            "enabled": False,
                            "message": "ระบบบันทึกถูกปิดไว้ชั่วคราว กรุณาติดต่อผู้ดูแลระบบ",
                        }))
                        return True
                # Optional squelch for the record gate (<= -120 = always record).
                # Coerce defensively: accept int/float/numeric string; ignore
                # anything that is not a number rather than crashing the handler
                # (a malformed squelch used to raise an empty-message exception).
                sq_raw = msg.get("squelch")
                sq = None
                if sq_raw is not None:
                    try:
                        sq = float(sq_raw)
                    except (TypeError, ValueError):
                        sq = None  # leave the listener's current record squelch
                if enabled:
                    audio_broadcaster.start_auto_record(listener, sq)
                else:
                    audio_broadcaster.stop_auto_record(listener)
                await websocket.send_text(json.dumps({
                    "type": "auto_record_ack",
                    "enabled": listener.auto_record,
                    "squelch": listener.record_squelch_db,
                }))
                logger_aud.info(
                    "🎙️ Listener %s auto-record → %s (squelch=%.1f dB)",
                    listener.id[:8],
                    "ON" if listener.auto_record else "OFF",
                    listener.record_squelch_db,
                )
            except Exception as e:
                logger_aud.warning(f"🔊 Bad auto_record control: {e or e.__class__.__name__}")
        return True

    # Persistent two-task loop: one long-lived task for receiving control
    # messages, one long-lived task for sending audio frames. Using a pair of
    # durable tasks (instead of cancelling/recreating receive_text() every
    # tick) avoids dropping control messages and removes per-tick task churn
    # from the audio hot path. The two tasks communicate via the listener's
    # output queue; the send task just drains it. A shared event coordinates
    # teardown: cancelling the recv task breaks the loop on disconnect.
    recv_task = asyncio.ensure_future(websocket.receive_text())
    try:
        while True:
            frame_task = asyncio.ensure_future(listener.queue.get())
            done, pending = await asyncio.wait(
                {recv_task, frame_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Only cancel the frame task (short-lived). The recv task is
            # long-lived: keep it across iterations so we never miss a
            # control message just because a frame arrived first.
            if frame_task in pending:
                frame_task.cancel()

            if recv_task in done:
                try:
                    msg = recv_task.result()
                    cont = await _handle_control(msg)
                    if not cont:
                        break
                    # Restart the long-lived recv task for the next message.
                    recv_task = asyncio.ensure_future(websocket.receive_text())
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger_aud.info(f"🔊 Audio WS recv error: {e}")
                    break

            if frame_task in done:
                try:
                    frame = frame_task.result()
                    # The queue carries two frame kinds:
                    #   - bytes        → AUD2 audio (binary WebSocket frame)
                    #   - ("sig", db)  → per-listener SIG meter update (text)
                    # Routing sig as a TEXT frame lets the client's audio
                    # ArrayBuffer path and its JSON text path stay separate,
                    # and avoids polluting the binary audio decoder.
                    if isinstance(frame, tuple):
                        try:
                            kind = frame[0]
                            if kind == "sig":
                                await websocket.send_text(
                                    json.dumps({"type": "sig", "db": frame[1]})
                                )
                        except Exception:
                            pass
                    else:
                        await websocket.send_bytes(frame)
                except Exception as e:
                    logger_aud.info(f"🔊 Audio WS send_bytes error: {e}")
                    break
    except WebSocketDisconnect:
        logger_aud.info("🔊 Audio WS client disconnected")
    except Exception as e:
        logger_aud.warning(f"🔊 Audio WS error: {e}")
    finally:
        recv_task.cancel()
        await audio_broadcaster.unsubscribe(listener)
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
    # Settings HTML is served to all authenticated users; access control is
    # enforced by:
    #   1) Frontend: settings.html shows admin-only tabs (SDR, Recording, STT,
    #      Telegram, Keywords, Users) only to admins. Non-admin users see the
    #      General (Timezone) and Display (Waterfall) tabs only.
    #   2) API: all config-mutating endpoints under /api/settings/* and
    #      /api/sdr/recording-config require admin (require_admin dependency).
    return FileResponse(str(frontend_dir / "pages" / "settings.html"))


@app.get("/admin/users")
async def serve_admin_users():
    return FileResponse(str(frontend_dir / "pages" / "admin" / "users.html"))