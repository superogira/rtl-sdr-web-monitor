/**
 * Live Audio Player - Web Audio API based PCM streaming player.
 *
 * Connects to /ws/audio WebSocket, receives binary PCM frames,
 * and plays them through AudioContext with buffer scheduling.
 *
 * Inspired by OpenWebRX client-side audio handling.
 */
const LiveAudio = {
    ws: null,
    ctx: null,
    gainNode: null,
    isPlaying: false,
    volume: 0.8,

    // Buffer scheduling
    _sampleRate: 8000,       // Updated from server frames
    _nextStartTime: 0,       // 0 = not started; set on first frame arrival
    _targetLatency: 0.45,    // 450ms target buffer ahead (jitter cushion)
                             // Increased from 250ms: absorbs network/CPU
                             // jitter better to reduce underrun stutter.
    _maxLatency: 1.0,        // 1000ms ceiling — if bufferAhead exceeds this,
                             // we RESYNC (drop the accumulated backlog) so the
                             // stream never drifts into seconds of latency
                             // (the "ฟลุด/ต้องกดฟังสดใหม่" symptom).
    _queue: [],              // Queue of decoded Float32Arrays
    _maxQueue: 30,           // Max buffers in queue (prevents memory leak)
    _framesReceived: 0,
    _underruns: 0,
    _resyncs: 0,             // Count of latency resyncs (for diagnostics)
    _reconnectTimer: null,
    _sourceNodes: [],        // Track active source nodes for cleanup

    // VU meter
    _analyser: null,
    _vuEl: null,

    // Heartbeat + diag
    _heartbeatTimer: null,
    _diagTimer: null,

    /**
     * Connect to audio WebSocket and start playing.
     * Auto-starts SDR streaming if not already streaming.
     */
    async start() {
        if (this.isPlaying) return;
        console.log("🔊 [audio] start() called");

        // === Ensure SDR is streaming first (audio needs demodulated output) ===
        if (typeof App === 'undefined') {
            console.error("🔊 [audio] App not available");
            this._updateStatus("❌ App?");
            return;
        }

        // Ensure connected
        if (!App.isConnected) {
            console.log("🔊 [audio] not connected → calling connectSDR()");
            this._updateStatus("🔗 เชื่อมต่อ...");
            try {
                await App.connectSDR();
            } catch (e) {
                console.error("🔊 [audio] connectSDR failed:", e);
                this._updateStatus("❌ ต่อไม่ได้");
                alert("เชื่อมต่อ RTL-TCP ไม่ได้: " + (e.message || e));
                return;
            }
            // connectSDR returns void; verify via status
            if (!App.isConnected) {
                console.error("🔊 [audio] connectSDR returned but still not connected");
                this._updateStatus("❌ ต่อไม่ได้");
                alert("เชื่อมต่อ RTL-TCP ไม่สำเร็จ (ดู log)");
                return;
            }
            console.log("🔊 [audio] connected OK");
        }

        // Ensure streaming
        if (!App.isStreaming) {
            console.log("🔊 [audio] not streaming → calling toggleStream()");
            this._updateStatus("📡 เริ่มรับ...");
            try {
                await App.toggleStream();
            } catch (e) {
                console.error("🔊 [audio] toggleStream failed:", e);
                this._updateStatus("❌ รับไม่ได้");
                return;
            }
            // Wait a moment for stream to actually start producing audio
            await new Promise(r => setTimeout(r, 800));
            if (!App.isStreaming) {
                console.warn("🔊 [audio] stream may not have started");
            }
        }

        // Create AudioContext (must be triggered by user gesture)
        if (!this.ctx) {
            try {
                this.ctx = new (window.AudioContext || window.webkitAudioContext)();
                this.gainNode = this.ctx.createGain();
                this.gainNode.gain.value = this.volume;
                this.gainNode.connect(this.ctx.destination);

                // Analyser for VU meter
                this._analyser = this.ctx.createAnalyser();
                this._analyser.fftSize = 256;
                this.gainNode.connect(this._analyser);
            } catch (e) {
                console.error("AudioContext creation failed:", e);
                alert("เบราว์เซอร์ไม่รองรับ Web Audio API");
                return;
            }
        }

        // Resume if suspended (autoplay policy)
        if (this.ctx.state === "suspended") {
            await this.ctx.resume();
        }

        this.isPlaying = true;
        this._framesReceived = 0;
        this._underruns = 0;
        this._resyncs = 0;
        this._nextStartTime = 0;  // Reset; will be set on first frame arrival
        this._connectWS();
        this._updateUI();
        this._startDebugOverlay();
        this._startHeartbeat();
        this._startDiag();
        console.log("🔊 [audio] start() complete");
    },

    /**
     * Stop audio playback and close connection.
     */
    stop() {
        console.log("🔊 [audio] stop() called");
        this.isPlaying = false;
        if (this.ws) {
            try { this.ws.close(); } catch (e) {}
            this.ws = null;
        }
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
        if (this._debugTimer) {
            clearInterval(this._debugTimer);
            this._debugTimer = null;
        }
        if (this._heartbeatTimer) {
            clearInterval(this._heartbeatTimer);
            this._heartbeatTimer = null;
        }
        if (this._diagTimer) {
            clearInterval(this._diagTimer);
            this._diagTimer = null;
        }
        this._queue = [];
        // Stop all active source nodes
        for (const node of this._sourceNodes) {
            try { node.stop(); } catch (e) {}
            try { node.disconnect(); } catch (e) {}
        }
        this._sourceNodes = [];
        this._updateUI();
        this._updateStatus("");
    },

    /**
     * Toggle play/stop.
     */
    toggle() {
        if (this.isPlaying) {
            this.stop();
        } else {
            this.start();
        }
    },

    /**
     * Set volume (0.0 to 1.0).
     */
    setVolume(vol) {
        this.volume = Math.max(0, Math.min(1, vol));
        if (this.gainNode) {
            this.gainNode.gain.value = this.volume;
        }
        localStorage.setItem("audio_volume", String(this.volume));
    },

    _connectWS() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        // Pass JWT token as query param so the server can identify the user
        // for per-user manual recording (admins bypass the recording toggle).
        const token = localStorage.getItem('access_token') || '';
        const url = `${protocol}//${window.location.host}/ws/audio${token ? '?token=' + encodeURIComponent(token) : ''}`;
        console.log("🔊 [audio] connecting WS:", url);

        try {
            this.ws = new WebSocket(url);
            this.ws.binaryType = "arraybuffer";

            this.ws.onopen = () => {
                console.log("🔊 [audio] WS onopen (readyState:", this.ws.readyState + ")");
                this._updateStatus("🟢");
            };

            this.ws.onmessage = (event) => {
                if (event.data instanceof ArrayBuffer) {
                    this._handleFrame(event.data);
                    if (this._framesReceived < 3) {
                        console.log(`🔊 [audio] frame #${this._framesReceived}: ${event.data.byteLength} bytes`);
                    }
                } else {
                    // Text message: diagnostic "ready", "pong", or control ack.
                    //
                    // NOTE: because we set binaryType="arraybuffer" above, text
                    // frames arrive as plain JS STRINGS — not Blobs. Calling
                    // event.data.text() only works for Blobs, which caused the
                    // "event.data.text is not a function" crash in Edge (and
                    // silently swallowed every record_ack / vfo_ack / squelch_ack).
                    // Handle both shapes for safety.
                    const handleText = (txt) => {
                        try {
                            const obj = JSON.parse(txt);
                            if (obj.type === "ready") {
                                console.log("🔊 [audio] server ready:", obj);
                                // Initialize local VFO offset from server default
                                if (obj.vfo_offset_hz !== undefined && typeof App !== 'undefined') {
                                    App._vfoOffsetHz = obj.vfo_offset_hz;
                                    App.updateFreqDisplay();
                                }
                            } else if (obj.type === "vfo_ack") {
                                // Server confirmed THIS listener's VFO change
                                if (typeof App !== 'undefined') {
                                    App._vfoOffsetHz = obj.vfo_offset_hz || 0;
                                    App.updateFreqDisplay();
                                }
                            } else if (obj.type === "record_ack") {
                                // Per-user manual recording acknowledgement
                                console.log("🎙️ [audio] record_ack:", obj);
                                if (typeof App !== 'undefined' && App._onRecordAck) {
                                    App._onRecordAck(obj);
                                }
                            } else if (obj.type === "auto_squelch_ack") {
                                // Server confirmed auto-squelch toggle.
                                console.log("🎚️ [audio] auto_squelch_ack:", obj);
                                if (typeof App !== 'undefined' && App._onAutoSquelchAck) {
                                    App._onAutoSquelchAck(obj);
                                }
                            } else {
                                console.log("🔊 [audio] server text:", txt);
                            }
                        } catch {
                            if (txt === "pong") {
                                // heartbeat response
                            } else {
                                console.log("🔊 [audio] server text:", txt);
                            }
                        }
                    };

                    if (typeof event.data === "string") {
                        handleText(event.data);
                    } else if (event.data && typeof event.data.text === "function") {
                        // Fallback: if a future change delivers Blobs, still work.
                        event.data.text().then(handleText);
                    } else {
                        console.warn("🔊 [audio] unknown text message type:", typeof event.data);
                    }
                }
            };

            this.ws.onclose = (ev) => {
                console.log(`🔊 [audio] WS onclose (code=${ev.code}, reason="${ev.reason}", wasClean=${ev.wasClean})`);
                // Code 1013 = server is full (max_listeners_reached).
                // Stop playback and show a friendly Thai message — do NOT
                // auto-reconnect, because the server will keep rejecting.
                if (ev.code === 1013) {
                    this._updateStatus("🔴 เต็ม");
                    // Reset isPlaying so stop() doesn't try to reconnect
                    this.isPlaying = false;
                    this._updateUI();
                    alert("ขณะนี้มีผู้ใช้งานเต็มจำนวนแล้ว กรุณาลองใหม่ในภายหลัง");
                    return;
                }
                this._updateStatus("🔴 " + ev.code);
                if (this.isPlaying) {
                    this._scheduleReconnect();
                }
            };

            this.ws.onerror = (err) => {
                console.error("🔊 [audio] WS onerror:", err);
                this._updateStatus("🔴 ERR");
            };
        } catch (e) {
            console.error("Audio WS connect error:", e);
            this._scheduleReconnect();
        }
    },

    /**
     * Send a JSON control message to THIS listener's demodulator (per-user).
     * Safe to call when WS not open (no-op).
     */
    sendControl(obj) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(obj));
                return true;
            } catch (e) {
                console.warn("🔊 [audio] sendControl failed:", e);
            }
        }
        return false;
    },

    /**
     * Set THIS client's listening frequency (absolute Hz) via the audio WS.
     * Returns true if sent over WS (per-listener), false if WS not open.
     */
    setVFO(freqHz) {
        return this.sendControl({ type: "vfo", frequency: freqHz });
    },

    /**
     * Set THIS client's demodulation mode via the audio WS.
     */
    setMode(mode) {
        return this.sendControl({ type: "mode", mode: String(mode).toUpperCase() });
    },

    /**
     * Set THIS client's channel bandwidth (Hz) via the audio WS.
     */
    setBandwidth(bwHz) {
        return this.sendControl({ type: "bandwidth", bandwidth: bwHz });
    },

    /**
     * Set THIS client's squelch threshold (dB) via the audio WS.
     *
     * When the demodulated channel power is below this level, the SERVER mutes
     * the live audio to digital silence — so the user hears a quiet "dead"
     * carrier instead of white-noise/static, exactly like a real radio.
     * Values <= -120 dB disable squelch (pass everything, including static).
     *
     * Returns true if sent over WS, false if WS not open.
     */
    setSquelch(levelDb) {
        return this.sendControl({ type: "squelch", level: Number(levelDb) });
    },

    /**
     * Toggle adaptive (auto) squelch for THIS listener. When on, the server
     * tracks the noise floor and opens/closes the gate automatically.
     */
    setAutoSquelch(enabled) {
        return this.sendControl({ type: "auto_squelch", enabled: Boolean(enabled) });
    },

    /**
     * Start manual recording for THIS listener (per-user).
     * Server responds with a record_ack message.
     */
    startRecording() {
        return this.sendControl({ type: "record_start" });
    },

    /**
     * Stop manual recording for THIS listener and save.
     * Server responds with a record_ack containing the saved recording info.
     */
    stopRecording() {
        return this.sendControl({ type: "record_stop" });
    },

    _scheduleReconnect() {
        if (this._reconnectTimer || !this.isPlaying) return;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            if (this.isPlaying) {
                console.log("🔊 [audio] reconnecting...");
                this._connectWS();
            }
        }, 2000);
    },

    /**
     * Heartbeat: send "ping" every 20s to keep WS alive
     */
    _startHeartbeat() {
        if (this._heartbeatTimer) clearInterval(this._heartbeatTimer);
        this._heartbeatTimer = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                try { this.ws.send("ping"); } catch (e) {}
            }
        }, 20000);
    },

    /**
     * Periodically fetch /api/audio/diag and log to console + status display.
     * Helps diagnose whether audio chunks are being produced server-side.
     */
    _startDiag() {
        if (this._diagTimer) clearInterval(this._diagTimer);
        let tick = 0;
        const fetchDiag = async () => {
            try {
                const res = await fetch("/api/audio/diag");
                const data = await res.json();
                tick++;
                if (tick <= 5 || this._framesReceived === 0) {
                    console.log(`🔊 [diag] ${JSON.stringify(data)}`);
                }
            } catch (e) {
                console.warn("🔊 [diag] fetch failed:", e);
            }
        };
        // First fetch immediately, then every 3s for the first 15s
        fetchDiag();
        this._diagTimer = setInterval(fetchDiag, 3000);
    },

    /**
     * G.711 µ-law decode table (256 entries). Built lazily on first use via
     * _buildUlawTable() using the canonical ITU-T G.711 decode formula.
     * Stores int16 PCM values; the frontend converts to Float32 for Web Audio.
     */
    _ulawTable: null,

    _buildUlawTable() {
        const t = new Int16Array(256);
        for (let i = 0; i < 256; i++) {
            let uval = ~i & 0xFF;            // bit-invert
            const sign = (uval & 0x80) !== 0;
            let exponent = (uval >> 4) & 0x07;
            let mantissa = uval & 0x0F;
            let sample = ((mantissa << 3) + 0x84) << exponent;
            sample -= 0x84;                  // remove bias
            t[i] = sign ? -sample : sample;
        }
        this._ulawTable = t;
        return t;
    },

    /**
     * Parse binary frame and schedule playback.
     * Supports two frame formats:
     *   - "AUD2" (preferred): 4B magic + 4B sample_rate LE + 4B num_samples LE
     *     + num_samples bytes of G.711 µ-law (1 byte/sample). Half the
     *     bandwidth of AUD1 at the same sample rate.
     *   - "AUD1" (legacy):    4B magic + 4B sample_rate LE + 4B num_samples LE
     *     + num_samples×2 bytes of PCM int16 LE.
     */
    _handleFrame(buffer) {
        if (!this.ctx || !this.isPlaying) return;

        const view = new DataView(buffer);

        // Validate magic
        const magic = String.fromCharCode(
            view.getUint8(0), view.getUint8(1),
            view.getUint8(2), view.getUint8(3)
        );
        if (magic !== "AUD1" && magic !== "AUD2") {
            console.warn("Invalid audio frame magic:", magic);
            return;
        }

        const sampleRate = view.getUint32(4, true);
        const numSamples = view.getUint32(8, true);

        // Update sample rate if changed (requires context recreation in extreme cases,
        // but AudioContext can resample, so we just note it)
        if (sampleRate !== this._sampleRate) {
            this._sampleRate = sampleRate;
        }

        const float32 = new Float32Array(numSamples);
        const offset = 12;  // After 12-byte header

        if (magic === "AUD2") {
            // µ-law: 1 byte/sample → Float32 via decode table
            const tbl = this._ulawTable || this._buildUlawTable();
            const bytes = new Uint8Array(buffer, offset, numSamples);
            for (let i = 0; i < numSamples; i++) {
                float32[i] = tbl[bytes[i]] / 32768;
            }
        } else {
            // AUD1: PCM int16 LE → Float32
            for (let i = 0; i < numSamples; i++) {
                const sample = view.getInt16(offset + i * 2, true);
                float32[i] = sample / 32768;
            }
        }

        // Create AudioBuffer
        const audioBuffer = this.ctx.createBuffer(1, numSamples, sampleRate);
        audioBuffer.copyToChannel(float32, 0);

        // Schedule playback
        this._scheduleBuffer(audioBuffer);
        this._framesReceived++;
    },

    /**
     * Schedule an AudioBuffer for seamless playback.
     * Uses a "next start time" cursor to avoid gaps/overlaps, with two
     * safety mechanisms:
     *
     * 1) UNDERRUN recovery — if the cursor has fallen behind `now` (we ran
     *    out of scheduled audio and there was a gap/silence), reset it to
     *    now + targetLatency to rebuild the jitter cushion.
     *
     * 2) RESYNC-ON-FLOOD — if the cursor has run TOO FAR ahead of `now`
     *    (bufferAhead > maxLatency), the client is accumulating a growing
     *    backlog (e.g. CPU couldn't keep up, then caught up and dumped a
     *    burst). Without intervention this manifests as ever-increasing
     *    latency — the "ฟลุด/ต้องกดฟังสดใหม่" symptom. We snap the cursor
     *    back to now + targetLatency so playback stays near real time.
     */
    _scheduleBuffer(audioBuffer) {
        const now = this.ctx.currentTime;

        // --- RESYNC-ON-FLOOD: backlog grew too large, snap back to real time ---
        const ahead = this._nextStartTime - now;
        if (this._nextStartTime !== 0 && ahead > this._maxLatency) {
            this._resyncs++;
            console.warn(
                `🔊 [audio] resync #${this._resyncs}: bufferAhead=${ahead.toFixed(3)}s ` +
                `> max ${this._maxLatency}s → snapping to ${this._targetLatency * 1000}ms`
            );
            this._nextStartTime = now + this._targetLatency;
        }

        // --- UNDERRUN recovery: cursor fell behind real time ---
        // First frame, or we ran out of scheduled audio (gap/silence).
        // Reset to now + targetLatency to rebuild the jitter cushion.
        if (this._nextStartTime === 0 || this._nextStartTime < now) {
            if (this._nextStartTime !== 0) {
                this._underruns++;
                console.warn(`🔊 [audio] underrun #${this._underruns}, re-buffering ${this._targetLatency * 1000}ms`);
            }
            this._nextStartTime = now + this._targetLatency;
        }

        // Prevent queue overflow (drop oldest scheduled-but-unstarted node)
        if (this._sourceNodes.length > this._maxQueue) {
            const old = this._sourceNodes.shift();
            try { old.stop(); } catch (e) {}
            try { old.disconnect(); } catch (e) {}
        }

        const source = this.ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.gainNode);
        source.start(this._nextStartTime);

        // Advance the cursor
        this._nextStartTime += audioBuffer.duration;

        // Cleanup after playback
        source.onended = () => {
            const idx = this._sourceNodes.indexOf(source);
            if (idx >= 0) this._sourceNodes.splice(idx, 1);
        };

        this._sourceNodes.push(source);
    },

    /**
     * Update UI buttons and status.
     */
    _updateUI() {
        const btn = document.getElementById("btn-listen");
        const indicator = document.getElementById("listen-indicator");
        if (btn) {
            if (this.isPlaying) {
                btn.textContent = "🔊 กำลังฟัง";
                btn.classList.add("active");
            } else {
                btn.textContent = "🔈 ฟังสด";
                btn.classList.remove("active");
            }
        }
        if (indicator) {
            indicator.style.display = this.isPlaying ? "" : "none";
        }
    },

    _updateStatus(icon) {
        const el = document.getElementById("audio-status");
        if (el) {
            el.textContent = icon;
        }
    },

    /**
     * Debug overlay — shows framesReceived, underruns, resyncs, sample rate.
     * Updated every 1 second. Helps diagnose audio pipeline issues.
     */
    _debugTimer: null,
    _startDebugOverlay() {
        if (this._debugTimer) clearInterval(this._debugTimer);
        this._debugTimer = setInterval(() => {
            if (!this.isPlaying) {
                clearInterval(this._debugTimer);
                this._debugTimer = null;
                return;
            }
            const stats = this.getStats();
            const el = document.getElementById("audio-status");
            if (el) {
                el.textContent = `${stats.framesReceived}f | ${stats.underruns}u | ${stats.resyncs}r | ${stats.bufferAhead}`;
                el.title = `frames: ${stats.framesReceived}, underruns: ${stats.underruns}, resyncs: ${stats.resyncs}, buffer: ${stats.bufferAhead}, nodes: ${stats.activeNodes}, sr: ${stats.sampleRate}Hz`;
            }
        }, 1000);
    },

    /**
     * Get current buffer stats for debugging UI.
     */
    getStats() {
        const now = this.ctx ? this.ctx.currentTime : 0;
        const bufferAhead = Math.max(0, this._nextStartTime - now);
        return {
            playing: this.isPlaying,
            framesReceived: this._framesReceived,
            underruns: this._underruns,
            resyncs: this._resyncs,
            bufferAhead: bufferAhead.toFixed(3) + "s",
            activeNodes: this._sourceNodes.length,
            sampleRate: this._sampleRate,
        };
    },
};