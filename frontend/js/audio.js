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
    _nextStartTime: 0,       // When the next buffer should play
    _queue: [],              // Queue of decoded Float32Arrays
    _maxQueue: 20,           // Max buffers in queue (prevents memory leak)
    _framesReceived: 0,
    _underruns: 0,
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
        this._nextStartTime = this.ctx.currentTime;
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
        const url = `${protocol}//${window.location.host}/ws/audio`;
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
                    // Text message: diagnostic "ready" or "pong"
                    event.data.text().then(txt => {
                        try {
                            const obj = JSON.parse(txt);
                            if (obj.type === "ready") {
                                console.log("🔊 [audio] server ready:", obj);
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
                    });
                }
            };

            this.ws.onclose = (ev) => {
                console.log(`🔊 [audio] WS onclose (code=${ev.code}, reason="${ev.reason}", wasClean=${ev.wasClean})`);
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
     * Parse binary frame and schedule playback.
     * Frame format: "AUD1" (4B) + sample_rate (4B LE) + num_samples (4B LE) + PCM int16 data
     */
    _handleFrame(buffer) {
        if (!this.ctx || !this.isPlaying) return;

        const view = new DataView(buffer);

        // Validate magic
        const magic = String.fromCharCode(
            view.getUint8(0), view.getUint8(1),
            view.getUint8(2), view.getUint8(3)
        );
        if (magic !== "AUD1") {
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

        // Decode PCM int16 → Float32
        const float32 = new Float32Array(numSamples);
        const offset = 12;  // After 12-byte header
        for (let i = 0; i < numSamples; i++) {
            const sample = view.getInt16(offset + i * 2, true);
            float32[i] = sample / 32768;
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
     * Uses a "next start time" cursor to avoid gaps/overlaps.
     */
    _scheduleBuffer(audioBuffer) {
        const now = this.ctx.currentTime;

        // If we're behind schedule (underrun), reset to now
        if (this._nextStartTime < now) {
            this._underruns++;
            this._nextStartTime = now + 0.05;  // Small buffer ahead
        }

        // Prevent queue overflow (drop oldest)
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
     * Debug overlay — shows framesReceived, underruns, sample rate.
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
                el.textContent = `${stats.framesReceived}f | ${stats.underruns}u | ${stats.bufferAhead}`;
                el.title = `frames: ${stats.framesReceived}, underruns: ${stats.underruns}, buffer: ${stats.bufferAhead}, nodes: ${stats.activeNodes}, sr: ${stats.sampleRate}Hz`;
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
            bufferAhead: bufferAhead.toFixed(3) + "s",
            activeNodes: this._sourceNodes.length,
            sampleRate: this._sampleRate,
        };
    },
};