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

    # Start the keyword-alert worker (DB queue consumer). Evaluates alert rules
    # against newly transcribed recordings and delivers notifications. Failure to
    # start is non-fatal — transcription still works, just no alerts go out.
    from backend.alerts.worker import alert_worker
    try:
        await alert_worker.start()
    except Exception as e:
        print(f"⚠️ AlertWorker start failed: {e}")

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
    from backend.alerts.worker import alert_worker
    try:
        await alert_worker.stop()
    except Exception as e:
        print(f"⚠️ AlertWorker stop error: {e}")


# Create FastAPI app
# Disable auto docs in production to avoid exposing API structure to attackers.
_is_prod = settings.APP_ENV.lower() in ("production", "prod")
app = FastAPI(
    title=settings.APP_NAME,
    description="RTL-SDR Web Monitor - Radio signal monitoring, recording, and transcription",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# Frontend is served from same origin, so not needed in most cases
# Only add for production with separate frontend server


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Disable browser/proxy caching for JS/CSS/HTML during development so
    edits are picked up on the next page load instead of serving stale files.

    The 'no-store' directive is the strongest cache-buster and works across
    Chrome/Firefox/Safari. We scope it to the asset paths only; API responses
    are left untouched.
    """
    path = request.url.path
    # Disable cache for all JS/CSS and ALL page routes (not just *.html),
    # because routes like /dashboard, /alert-rules, /settings etc. serve
    # HTML via FileResponse but don't end with .html in the URL.
    _PAGE_ROUTES = {
        "/", "/login", "/dashboard", "/recordings", "/settings",
        "/alert-rules", "/alerts", "/stats", "/admin/users", "/waterfall-test",
    }
    if path.startswith("/js/") or path.startswith("/css/") or path.endswith(".html") or path in _PAGE_ROUTES:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    return await call_next(request)


# === API Routes ===
app.include_router(auth_router)
app.include_router(sdr_router)
app.include_router(recordings_router)
app.include_router(settings_router)
app.include_router(devices_router)
app.include_router(recording_jobs_router)

# Keyword alerting routers (keywords, channels, rules, alerts, stats).
from backend.alerts.router import (
    keyword_groups_router, keywords_router, channels_router,
    notif_groups_router, alert_rules_router, alerts_router, alert_stats_router,
)
app.include_router(keyword_groups_router)
app.include_router(keywords_router)
app.include_router(channels_router)
app.include_router(notif_groups_router)
app.include_router(alert_rules_router)
app.include_router(alerts_router)
app.include_router(alert_stats_router)


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
    """Main WebSocket endpoint for real-time updates (requires auth)."""
    import asyncio
    import json
    import logging
    from backend.sdr.manager import sdr_manager
    from backend.audio.broadcaster import audio_broadcaster
    from backend.auth.jwt import decode_token

    logger_ws = logging.getLogger("websocket")

    # === Authenticate via query param ?token=JWT (same as /ws/audio) ===
    token = websocket.query_params.get("token")
    if not token:
        # Reject unauthenticated connections
        await websocket.close(code=4401, reason="Authentication required")
        return
    try:
        payload = decode_token(token)
        if not payload or payload.get("type") != "access":
            await websocket.close(code=4401, reason="Invalid or expired token")
            return
    except Exception:
        await websocket.close(code=4401, reason="Invalid token")
        return

    try:
        await websocket.accept()
        logger_ws.info("🔌 WebSocket client connected (auth OK)")
    except Exception as e:
        logger_ws.error(f"WebSocket accept failed: {e}")
        return

    # Resolve the live runtime for this connection's device_id (if any).
    # Per-device runtimes allow different browser tabs to listen to different
    # physical SDR devices simultaneously. Falls back to the global singleton.
    from backend.sdr.live_runtime import live_runtimes
    ws_device_id = websocket.query_params.get("device_id")
    try:
        mgr, bcast = await live_runtimes.get(ws_device_id)
    except Exception as e:
        logger_ws.warning(f"WebSocket device {ws_device_id} not available: {e}")
        await websocket.close(code=4404, reason=f"Device not available: {e}")
        return

    # Register spectrum callback (per-connection; unregistered on disconnect
    # so a closed socket doesn't accumulate dead callbacks forever).
    spectrum_data = {"latest": None}

    def on_spectrum(data):
        spectrum_data["latest"] = data

    mgr.on_spectrum(on_spectrum)

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

    mgr.on_new_recording(on_new_recording)

    # Per-connection VFO cursor offset for non-live SIG. When a client is NOT
    # listening live it sends its cursor offset here; the status loop computes
    # the channel power at that offset from the latest IQ so the client's VFO A
    # bar reflects the frequency under its cursor (not just the global center).
    # None means "use the global center level" (default / legacy behavior).
    cursor_state = {"offset_hz": None}

    async def _recv_loop():
        """Drain client→server control messages (currently just vfo_cursor)."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "vfo_cursor":
                    try:
                        cursor_state["offset_hz"] = float(msg.get("offset_hz", 0.0))
                    except (TypeError, ValueError):
                        pass
        except Exception:
            # Disconnect / closed — the main loop will also unwind.
            return

    recv_task = asyncio.ensure_future(_recv_loop())

    status_counter = 0
    sig_cursor_counter = 0
    # Per-iteration interval is ~33ms (the loop's asyncio.sleep). 6 iterations
    # ≈ 200ms = 5 FPS for the lightweight per-cursor SIG frame (vs the full
    # status frame at 1 FPS). 5 FPS keeps the bar responsive without flooding;
    # each computation is ~1ms (single-shot FFT).
    SIG_CURSOR_INTERVAL = 6
    try:
        while True:
            # Send spectrum data as a BINARY WebSocket frame.
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

            # Lightweight per-cursor SIG at 5 FPS (only when the client has
            # declared a cursor AND is not listening live — live listeners get
            # per-listener 'sig' frames over /ws/audio). Much more responsive
            # than waiting for the 1 FPS full-status frame.
            sig_cursor_counter += 1
            off = cursor_state["offset_hz"]
            if off is not None and sig_cursor_counter >= SIG_CURSOR_INTERVAL \
                    and not bcast.has_listeners:
                sig_cursor_counter = 0
                lvl = mgr.cursor_signal_level(off)
                if lvl is not None:
                    await websocket.send_text(json.dumps({
                        "type": "sig_cursor", "data": {"db": round(lvl, 1)},
                    }))

            # Send status at ~1 FPS (every 30th iteration)
            status_counter += 1
            if status_counter >= 30:
                status_counter = 0
                status = mgr.status
                # If this client has declared a VFO cursor AND is not listening
                # live, override the global signal_level_db with the channel
                # power at its cursor offset (so VFO A shows the freq under the
                # cursor, not the center). Live listeners get per-listener 'sig'
                # frames over /ws/audio instead, so this is only for non-live.
                off = cursor_state["offset_hz"]
                if off is not None and not bcast.has_listeners:
                    lvl = mgr.cursor_signal_level(off)
                    if lvl is not None:
                        status["signal_level_db"] = round(lvl, 1)
                await websocket.send_text(json.dumps({"type": "status", "data": status}))

            await asyncio.sleep(0.033)  # ~30 FPS
    except Exception as e:
        logger_ws.info(f"🔌 WebSocket disconnected: {e}")
    finally:
        recv_task.cancel()
        # Always unregister so callbacks don't leak across reconnects.
        mgr.off_spectrum(on_spectrum)
        mgr.off_new_recording(on_new_recording)


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
    from backend.auth.jwt import decode_token
    from backend.services.app_settings import get_setting

    logger_aud = logging.getLogger("audio_ws")

    # === Authenticate the WS connection via query param ?token=JWT (required) ===
    token = websocket.query_params.get("token")
    ws_user_id = None
    ws_user_role = None
    if not token:
        await websocket.close(code=4401, reason="Authentication required")
        return
    try:
        payload = decode_token(token)
        if not payload or payload.get("type") != "access":
            await websocket.close(code=4401, reason="Invalid or expired token")
            return
        ws_user_id = payload.get("sub")
        ws_user_role = payload.get("role")
    except Exception:
        await websocket.close(code=4401, reason="Invalid token")
        return

    try:
        await websocket.accept()
    except Exception as e:
        logger_aud.error(f"🔊 Audio WS accept failed: {e}")
        return

    # Resolve the live runtime for this connection's device_id (if any).
    from backend.sdr.live_runtime import live_runtimes
    ws_device_id = websocket.query_params.get("device_id")
    try:
        sdr_manager, audio_broadcaster = await live_runtimes.get(ws_device_id)
    except Exception as e:
        logger_aud.warning(f"Audio WS device {ws_device_id} not available: {e}")
        await websocket.close(code=4404, reason=f"Device not available: {e}")
        return
    mgr = sdr_manager  # alias to match _handle_control closure below

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
        f"🔊 Audio WS connected (device={ws_device_id}, listeners: {audio_broadcaster.listener_count}, "
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
            "device_id": ws_device_id,
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
                    #   - ("sig", {db, sq_open})  → per-listener SIG meter
                    #     update (text). Carries the smoothed signal level AND
                    #     the current squelch-gate state so the client can show
                    #     a "RX active" indicator (flashing green) on the VFO
                    #     row when audio is passing.
                    # Routing sig as a TEXT frame lets the client's audio
                    # ArrayBuffer path and its JSON text path stay separate,
                    # and avoids polluting the binary audio decoder.
                    if isinstance(frame, tuple):
                        try:
                            kind = frame[0]
                            if kind == "sig":
                                payload = frame[1]
                                # payload is a dict: {db, sq_open}.
                                await websocket.send_text(
                                    json.dumps({"type": "sig", **payload})
                                )
                            elif kind == "notice":
                                # Server-initiated recording notice (manual
                                # max-duration cap, saved-on-disconnect, …).
                                # Forwarded verbatim; the client treats it as a
                                # regular record_ack.
                                payload = frame[1]
                                if isinstance(payload, dict):
                                    await websocket.send_text(json.dumps(payload))
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


@app.get("/alert-rules")
async def serve_alert_rules():
    return FileResponse(str(frontend_dir / "pages" / "alert_rules.html"))


@app.get("/alerts")
async def serve_alerts():
    return FileResponse(str(frontend_dir / "pages" / "alerts.html"))


@app.get("/stats")
async def serve_stats():
    return FileResponse(str(frontend_dir / "pages" / "stats.html"))


@app.get("/admin/users")
async def serve_admin_users():
    return FileResponse(str(frontend_dir / "pages" / "admin" / "users.html"))