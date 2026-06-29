/**
 * Live Audio Player - Web Audio API based PCM streaming player.
 *
 * Connects to /ws/audio WebSocket(s), receives binary PCM frames, and plays
 * them through AudioContext with buffer scheduling.
 *
 * MULTI-VFO (Dual VFO) architecture
 * ----------------------------------
 * Each VFO (A / B) is an independent listener on the server (one /ws/audio
 * connection each → its own demodulator / VFO offset / squelch / recording).
 * On the client, every VFO owns its own:
 *   - WebSocket connection + receive handling
 *   - audio source nodes queue + playback cursor (nextStartTime)
 *   - StereoPannerNode (routes to left / right / both speakers)
 *   - SIG level, recording/auto-record state
 * They all share ONE AudioContext + master gainNode + analyser (a single
 * output device). This lets one dashboard listen to two frequencies at once,
 * panned L/R so the operator can tell them apart by ear.
 *
 * The `LiveAudio` object below is the manager. It exposes a backwards-
 * compatible facade (setVFO / setSquelch / isPlaying / volume / toggle ...)
 * that operates on the ACTIVE VFO, so existing call sites in app.js keep
 * working. Dual-VFO-specific UI drives the manager directly.
 */

// Per-VFO speaker routing → StereoPannerNode pan value.
//   'left'  → -1   'both' → 0   'right' → +1
const SPEAKER_PAN = { left: -1, both: 0, right: 1 };
// Maximum number of simultaneous VFOs (A/B/C/D). Each active VFO is one
// server-side listener, so this also bounds the per-client listener cost.
const MAX_VFOS = 4;

const LiveAudio = {
    // === Shared audio graph (one output device for all VFOs) ===
    ctx: null,
    masterGain: null,    // master volume → destination
    _analyser: null,     // VU tap (read-only leaf)
    volume: 0.8,
    // === VFO registry ===
    // Each entry: { id, ws, sourceNodes, nextStartTime, sampleRate,
    //   framesReceived, underruns, resyncs, isPlaying, panner, speaker,
    //   reconnectTimer, heartbeatTimer, sigDb }
    _vfos: [],
    _activeIdx: 0,       // which VFO app.js controls (clicks, sliders) target
    _MAX_VFOS: MAX_VFOS, // max simultaneous VFOs (A/B/C/D)
    // Shared diag timer — only one for the whole manager.
    _debugTimer: null,

    // === Backwards-compat facade properties ===
    // `isPlaying` reflects the ACTIVE VFO so existing code works; for dual
    // mode use isVfoPlaying(idx).
    get isPlaying() {
        const v = this._activeVfo();
        return v ? v.isPlaying : false;
    },
    get ws() {
        const v = this._activeVfo();
        return v ? v.ws : null;
    },
    // Expose the active VFO's scheduling fields for legacy code paths.
    get _nextStartTime() { const v = this._activeVfo(); return v ? v.nextStartTime : 0; },
    get _framesReceived() { const v = this._activeVfo(); return v ? v.framesReceived : 0; },

    // ------------------------------------------------------------------
    // VFO access helpers
    // ------------------------------------------------------------------
    _activeVfo() { return this._vfos[this._activeIdx] || null; },

    /** Number of currently-live (streaming) VFOs. */
    get liveCount() { return this._vfos.filter(v => v && v.isPlaying).length; },
    /** True if ANY VFO is live. */
    get anyPlaying() { return this.liveCount > 0; },

    isVfoPlaying(idx) { const v = this._vfos[idx]; return !!(v && v.isPlaying); },

    setActive(idx) {
        if (idx < 0 || idx >= this._vfos.length) return;
        if (!this._vfos[idx].enabled) return;  // can't activate a disabled VFO
        this._activeIdx = idx;
        this._updateUI();
        if (typeof App !== 'undefined' && App.updateFreqDisplay) App.updateFreqDisplay();
    },

    get activeIdx() { return this._activeIdx; },

    /** Set how many VFOs are active (1-4). Replaces the old single/dual string
     * mode. When increasing while live, the newly-enabled VFOs auto-start so
     * the operator doesn't have to re-press ฟังสด. When decreasing, the
     * extra VFOs are stopped. */
    setVfoCount(n) {
        n = Math.max(1, Math.min(this._MAX_VFOS, n|0));
        this._ensureVfo(this._MAX_VFOS - 1);  // make sure all slots exist
        const wasPlaying = this.anyPlaying;
        // Mark enabled flags.
        for (let i = 0; i < this._vfos.length; i++) {
            this._vfos[i].enabled = (i < n);
        }
        // If live, start any newly-enabled VFO that isn't playing, and stop
        // any newly-disabled VFO that is playing.
        if (wasPlaying) {
            for (let i = 0; i < this._vfos.length; i++) {
                const v = this._vfos[i];
                if (v.enabled && !v.isPlaying) {
                    this.startVfo(i).catch(e => console.warn(`VFO ${v.id} autostart failed:`, e));
                } else if (!v.enabled && v.isPlaying) {
                    this.stopVfo(i);
                }
            }
        }
        // Keep active index in range.
        if (this._activeIdx >= n) this._activeIdx = n - 1;
        this._updateUI();
        if (typeof App !== 'undefined' && App.updateFreqDisplay) App.updateFreqDisplay();
    },

    get vfoCount() {
        return this._vfos.filter(v => v && v.enabled).length;
    },
    /** Backwards-compat: 'single' if count==1 else 'multi'. */
    get mode() { return this.vfoCount > 1 ? 'multi' : 'single'; },
    /** Legacy alias kept for any caller using setVfoMode. */
    setVfoMode(mode) {
        if (mode === 'single') this.setVfoCount(1);
        else if (mode === 'dual') this.setVfoCount(2);
    },

    /** Ensure a VFO slot exists (lazy-creates the registry entry). */
    _ensureVfo(idx) {
        while (this._vfos.length <= idx) {
            // VFO identity by index: A, B, C, D, ... (so VFO 2 = 'C', not 'B').
            const id = String.fromCharCode(65 + this._vfos.length);
            this._vfos.push(this._newVfoState(id));
        }
        return this._vfos[idx];
    },

    _newVfoState(id) {
        return {
            id,
            enabled: (id === 'A'),  // only VFO A is enabled by default
            ws: null,
            sourceNodes: [],
            nextStartTime: 0,
            sampleRate: 8000,
            framesReceived: 0,
            underruns: 0,
            resyncs: 0,
            isPlaying: false,
            panner: null,         // StereoPannerNode (created on start)
            muteNode: null,       // GainNode (gain 0 when muted) — created on start
            muted: false,         // per-VFO mute flag (persisted across reconnects)
            speaker: 'both',      // 'left' | 'both' | 'right'
            reconnectTimer: null,
            heartbeatTimer: null,
            sigDb: -120,
        };
    },

    // ------------------------------------------------------------------
    // Shared audio graph
    // ------------------------------------------------------------------
    /** Lazily create the shared AudioContext + master gain + analyser.
     * Must be called from a user gesture (the listen button). */
    _ensureContext() {
        if (this.ctx) return true;
        try {
            this.ctx = new (window.AudioContext || window.webkitAudioContext)();
            this.masterGain = this.ctx.createGain();
            this.masterGain.gain.value = this.volume;
            this.masterGain.connect(this.ctx.destination);
            this._analyser = this.ctx.createAnalyser();
            this._analyser.fftSize = 256;
            this.masterGain.connect(this._analyser);
            return true;
        } catch (e) {
            console.error("AudioContext creation failed:", e);
            alert(I18n.t('audio.noWebAudio'));
            return false;
        }
    },

    /** Create (or reset) a VFO's audio chain: mute gain → panner → master.
     *
     * Graph per VFO:  BufferSource → muteNode (gain 0/1) → panner → masterGain
     * The muteNode is a dedicated GainNode so muting is independent of the
     * master volume and of speaker routing. */
    _ensurePanner(v) {
        if (!this.ctx) return;
        if (v.panner) { try { v.panner.disconnect(); } catch (e) {} }
        if (v.muteNode) { try { v.muteNode.disconnect(); } catch (e) {} }
        v.muteNode = this.ctx.createGain();
        v.muteNode.gain.value = v.muted ? 0 : 1;
        v.panner = this.ctx.createStereoPanner();
        const pan = SPEAKER_PAN[v.speaker];
        v.panner.pan.value = (pan === undefined) ? 0 : pan;
        // Chain: muteNode → panner → masterGain.
        v.muteNode.connect(v.panner);
        v.panner.connect(this.masterGain);
    },

    // ------------------------------------------------------------------
    // Public: start / stop / toggle
    // ------------------------------------------------------------------

    /** Start the ACTIVE VFO first, then every other ENABLED VFO that isn't
     * already playing. VFOs beyond the active count are not started. */
    async start() {
        await this.startVfo(this._activeIdx);
        for (let i = 0; i < this._vfos.length; i++) {
            const v = this._vfos[i];
            if (v && v.enabled && i !== this._activeIdx && !v.isPlaying) {
                await this.startVfo(i);
            }
        }
    },

    async startVfo(idx) {
        const v = this._ensureVfo(idx);
        if (v.isPlaying) return;
        console.log(`🔊 [audio] VFO ${v.id} start()`);

        // Shared precondition: ensure SDR connected + streaming (done once at
        // manager level so two VFOs don't double-trigger connect/stream).
        if (typeof App === 'undefined') { this._updateStatus("❌ App?"); return; }
        if (!App.isConnected) {
            this._updateStatus(I18n.t('audio.connecting'));
            try { await App.connectSDR(); } catch (e) {
                this._updateStatus(I18n.t('audio.connectFail'));
                alert(I18n.t('audio.connectFailMsg') + (e.message || e));
                return;
            }
            if (!App.isConnected) { this._updateStatus(I18n.t('audio.notConnected')); return; }
        }
        if (!App.isStreaming) {
            this._updateStatus(I18n.t('audio.starting'));
            try { await App.toggleStream(); } catch (e) { this._updateStatus(I18n.t('audio.startFail')); return; }
            await new Promise(r => setTimeout(r, 800));
        }

        if (!this._ensureContext()) return;
        if (this.ctx.state === "suspended") await this.ctx.resume();
        this._ensurePanner(v);

        v.isPlaying = true;
        v.framesReceived = 0;
        v.underruns = 0;
        v.resyncs = 0;
        v.nextStartTime = 0;
        this._connectWS(idx);

        this._updateUI();
        this._startDebugOverlay();
        this._startHeartbeat(idx);
        console.log(`🔊 [audio] VFO ${v.id} start() complete`);
    },

    /** Stop every VFO that is currently playing. */
    stop() {
        for (let i = 0; i < this._vfos.length; i++) {
            if (this._vfos[i] && this._vfos[i].isPlaying) this.stopVfo(i);
        }
    },

    stopVfo(idx) {
        const v = this._vfos[idx];
        if (!v) return;
        console.log(`🔊 [audio] VFO ${v.id} stop()`);
        v.isPlaying = false;
        if (v.ws) { try { v.ws.close(); } catch (e) {} v.ws = null; }
        if (v.reconnectTimer) { clearTimeout(v.reconnectTimer); v.reconnectTimer = null; }
        if (v.heartbeatTimer) { clearInterval(v.heartbeatTimer); v.heartbeatTimer = null; }
        for (const node of v.sourceNodes) {
            try { node.stop(); } catch (e) {}
            try { node.disconnect(); } catch (e) {}
        }
        v.sourceNodes = [];
        this._updateUI();
        if (!this.anyPlaying) this._updateStatus("");
    },

    toggle() { this.anyPlaying ? this.stop() : this.start(); },

    // ------------------------------------------------------------------
    // Speaker routing (per VFO)
    // ------------------------------------------------------------------
    /** Set speaker routing for a VFO. mode: 'left' | 'both' | 'right'. */
    setSpeaker(idx, mode) {
        const v = this._vfos[idx];
        if (!v) return;
        if (!(mode in SPEAKER_PAN)) return;
        v.speaker = mode;
        if (v.panner) v.panner.pan.value = SPEAKER_PAN[mode];
        console.log(`🔊 [audio] VFO ${v.id} speaker → ${mode}`);
    },
    /** Backwards-compat: set speaker on the active VFO. */
    setSpeakerActive(mode) { this.setSpeaker(this._activeIdx, mode); },
    getSpeaker(idx) { const v = this._vfos[idx]; return v ? v.speaker : 'both'; },

    // ------------------------------------------------------------------
    // Per-VFO mute (independent of speaker routing & master volume)
    // ------------------------------------------------------------------
    /** Mute/unmute a VFO. When muted the VFO's audio is fully silenced
     * (muteNode gain → 0) regardless of its speaker routing. The flag is kept
     * on the VFO state so it survives a WS reconnect / restart. */
    setMute(idx, on) {
        const v = this._vfos[idx];
        if (!v) return;
        v.muted = !!on;
        if (v.muteNode) v.muteNode.gain.value = v.muted ? 0 : 1;
        console.log(`🔊 [audio] VFO ${v.id} ${v.muted ? 'MUTED' : 'unmuted'}`);
    },
    toggleMute(idx) {
        const v = this._vfos[idx];
        if (!v) return false;
        this.setMute(idx, !v.muted);
        return v.muted;
    },
    isMuted(idx) { const v = this._vfos[idx]; return v ? v.muted : false; },

    // ------------------------------------------------------------------
    // Per-VFO SOLO / Priority (client-side)
    // ------------------------------------------------------------------
    // When a VFO is "soloed", ONLY that VFO is audible; every other enabled
    // VFO is auto-muted. The original mute state of each VFO is snapshotted
    // before soloing so exiting solo restores the user's prior per-VFO mute
    // settings (open stays open, closed stays closed).
    /** Index of the currently-soloed VFO, or null if no solo is active. */
    _soloIdx: null,
    /** Snapshot of per-VFO `muted` flags captured when entering solo, used to
     *  restore the previous mute state when leaving solo. */
    _soloSavedMuted: [],

    /** Enter solo mode for a VFO: snapshot everyone's mute state, then unmute
     *  only this VFO and mute all other enabled VFOs. If `idx` is already the
     *  soloed VFO, this exits solo (toggle). */
    soloVfo(idx) {
        if (!this._vfos[idx]) return;
        // Toggle off if already soloing this VFO.
        if (this._soloIdx === idx) { this.exitSolo(); return; }
        // If switching solo to a different VFO, first restore the previous
        // snapshot so the new snapshot reflects the user's real preferences.
        if (this._soloIdx !== null) this._restoreSoloSnapshot();
        this._soloIdx = idx;
        // Snapshot current mute flags (so exitSolo can restore them exactly).
        this._soloSavedMuted = this._vfos.map(v => v ? !!v.muted : false);
        // Unmute the soloed VFO, mute every other enabled VFO.
        for (let i = 0; i < this._vfos.length; i++) {
            const v = this._vfos[i];
            if (!v || !v.enabled) continue;
            this.setMute(i, i !== idx);
        }
        console.log(`🔊 [audio] SOLO → VFO ${this._vfos[idx].id} (others muted)`);
    },

    /** Exit solo mode and restore each VFO's mute state to what it was before
     *  soloing. Safe to call when no solo is active (no-op). */
    exitSolo() {
        if (this._soloIdx === null) return;
        console.log(`🔊 [audio] SOLO off (restoring mute state)`);
        this._restoreSoloSnapshot();
        this._soloIdx = null;
        this._soloSavedMuted = [];
    },

    /** Internal: apply the snapshotted mute flags back to each VFO. */
    _restoreSoloSnapshot() {
        for (let i = 0; i < this._vfos.length; i++) {
            if (!this._vfos[i]) continue;
            const saved = this._soloSavedMuted[i];
            this.setMute(i, saved === undefined ? false : saved);
        }
    },

    /** True if `idx` is the currently-soloed VFO. */
    isSolo(idx) { return this._soloIdx === idx; },
    /** True if any VFO is currently soloed. */
    get anySolo() { return this._soloIdx !== null; },
    /** The index of the soloed VFO, or null. */
    get soloIdx() { return this._soloIdx; },

    // ------------------------------------------------------------------
    // Volume (shared master)
    // ------------------------------------------------------------------
    setVolume(vol) {
        this.volume = Math.max(0, Math.min(1, vol));
        // When muted, keep the stored volume but leave the gain at 0.
        if (!this._muted && this.masterGain) {
            this.masterGain.gain.value = this.volume;
        }
        localStorage.setItem("audio_volume", String(this.volume));
    },

    /** Mute toggle. Muting preserves the real volume so unmuting restores it
     *  and never overwrites the saved volume in localStorage. */
    get muted() { return !!this._muted; },
    setMuted(muted) {
        muted = !!muted;
        this._muted = muted;
        if (this.masterGain) {
            this.masterGain.gain.value = muted ? 0 : (this.volume || 0);
        }
    },

    // ------------------------------------------------------------------
    // Control messages (per-VFO via the VFO's own WS)
    // ------------------------------------------------------------------
    _sendControlVfo(idx, obj) {
        const v = this._vfos[idx];
        if (v && v.ws && v.ws.readyState === WebSocket.OPEN) {
            try { v.ws.send(JSON.stringify(obj)); return true; }
            catch (e) { console.warn(`🔊 [audio] VFO ${v.id} sendControl failed:`, e); }
        }
        return false;
    },
    /** Send a control message to the ACTIVE VFO's WS. (backwards-compat) */
    sendControl(obj) { return this._sendControlVfo(this._activeIdx, obj); },

    /** Send a control message to ALL live VFOs. Used for settings that apply
     * to every VFO at once (squelch, mode, bandwidth, auto-record, auto-
     * squelch) — the UI has a single SQL slider / AUTO button, so in dual mode
     * both VFOs must receive it or the non-active one stays stale (e.g. SQL
     * stuck, auto-record not running on VFO B). Returns true if sent to any. */
    sendControlAll(obj) {
        let any = false;
        for (let i = 0; i < this._vfos.length; i++) {
            if (this._sendControlVfo(i, obj)) any = true;
        }
        return any;
    },

    setVFO(freqHz) { return this.sendControl({ type: "vfo", frequency: freqHz }); },
    setVfoVfo(idx, freqHz) { return this._sendControlVfo(idx, { type: "vfo", frequency: freqHz }); },
    // mode/bandwidth/squelch/auto-* apply to ALL VFOs (shared UI controls).
    setMode(mode) { return this.sendControlAll({ type: "mode", mode: String(mode).toUpperCase() }); },
    setBandwidth(bwHz) { return this.sendControlAll({ type: "bandwidth", bandwidth: bwHz }); },
    setSquelch(levelDb) { return this.sendControlAll({ type: "squelch", level: Number(levelDb) }); },
    setAutoSquelch(enabled) { return this.sendControlAll({ type: "auto_squelch", enabled: Boolean(enabled) }); },
    setAutoRecord(enabled, squelchDb) {
        const obj = { type: "auto_record", enabled: Boolean(enabled) };
        if (squelchDb !== undefined && squelchDb !== null) obj.squelch = Number(squelchDb);
        return this.sendControlAll(obj);
    },
    startRecording() { return this.sendControl({ type: "record_start" }); },
    stopRecording() { return this.sendControl({ type: "record_stop" }); },

    // ------------------------------------------------------------------
    // WebSocket (one per VFO)
    // ------------------------------------------------------------------
    _connectWS(idx) {
        const v = this._vfos[idx];
        if (!v) return;
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const token = localStorage.getItem('access_token') || '';
        const url = `${protocol}//${window.location.host}/ws/audio${token ? '?token=' + encodeURIComponent(token) : ''}`;
        console.log(`🔊 [audio] VFO ${v.id} connecting WS:`, url);

        try {
            v.ws = new WebSocket(url);
            v.ws.binaryType = "arraybuffer";

            v.ws.onopen = () => {
                this._updateStatus("🟢");
                // Claim THIS listener at this VFO's cursor so it does not fall
                // back to the server's global VFO (center).
                if (typeof App !== 'undefined') {
                    const center = App.currentFreq || 0;
                    const off = (typeof App._vfoOffsetFor === 'function')
                        ? (App._vfoOffsetFor(idx) || 0)
                        : (App._vfoOffsetHz || 0);
                    if (center > 0) {
                        this._sendControlVfo(idx, { type: "vfo", frequency: Math.round(center + off) });
                    }
                    // Push THIS user's pending squelch so the listener starts
                    // with the user's own SQL (independent of the global value
                    // or what other users have set). Falls back to the current
                    // slider value if no explicit pending value was stored.
                    let sqlLevel = App._pendingSquelch;
                    if (sqlLevel === undefined || sqlLevel === null) {
                        const si = document.getElementById('squelch-input');
                        sqlLevel = si ? parseFloat(si.value) : null;
                    }
                    if (sqlLevel !== null && !isNaN(sqlLevel)) {
                        this._sendControlVfo(idx, { type: "squelch", level: Number(sqlLevel) });
                    }
                }
            };

            v.ws.onmessage = (event) => {
                if (event.data instanceof ArrayBuffer) {
                    this._handleFrame(idx, event.data);
                } else {
                    this._handleText(idx, event.data);
                }
            };

            v.ws.onclose = (ev) => {
                console.log(`🔊 [audio] VFO ${v.id} WS onclose (code=${ev.code})`);
                if (ev.code === 1013) {
                    this._updateStatus(I18n.t('audio.full'));
                    v.isPlaying = false;
                    this._updateUI();
                    alert(I18n.t('audio.fullMsg'));
                    return;
                }
                this._updateStatus("🔴 " + ev.code);
                if (v.isPlaying) this._scheduleReconnect(idx);
            };

            v.ws.onerror = (err) => {
                console.error(`🔊 [audio] VFO ${v.id} WS onerror:`, err);
                this._updateStatus(I18n.t('audio.connError'));
            };
        } catch (e) {
            console.error(`VFO ${v.id} WS connect error:`, e);
            this._scheduleReconnect(idx);
        }
    },

    _scheduleReconnect(idx) {
        const v = this._vfos[idx];
        if (!v || v.reconnectTimer || !v.isPlaying) return;
        v.reconnectTimer = setTimeout(() => {
            v.reconnectTimer = null;
            if (v.isPlaying) {
                console.log(`🔊 [audio] VFO ${v.id} reconnecting...`);
                this._connectWS(idx);
            }
        }, 2000);
    },

    _startHeartbeat(idx) {
        const v = this._vfos[idx];
        if (!v) return;
        if (v.heartbeatTimer) clearInterval(v.heartbeatTimer);
        v.heartbeatTimer = setInterval(() => {
            if (v.ws && v.ws.readyState === WebSocket.OPEN) {
                try { v.ws.send("ping"); } catch (e) {}
            }
        }, 20000);
    },

    // ------------------------------------------------------------------
    // Frame handling (per VFO)
    // ------------------------------------------------------------------
    _handleText(idx, raw) {
        const handleText = (txt) => {
            try {
                const obj = JSON.parse(txt);
                if (obj.type === "ready") {
                    if (typeof App !== 'undefined' && App._onVfoReady) App._onVfoReady(idx, obj);
                } else if (obj.type === "sig") {
                    const v = this._vfos[idx];
                    if (v) v.sigDb = obj.db;
                    // sq_open (squelch gate open) tells the UI whether this VFO
                    // is currently producing audio (signal > squelch). Used to
                    // flash the VFO row green so the operator can see which
                    // frequency is active at a glance.
                    if (typeof App !== 'undefined' && App._onVfoSig) {
                        App._onVfoSig(idx, obj.db, !!obj.sq_open);
                    }
                } else if (obj.type === "vfo_ack") {
                    if (typeof App !== 'undefined' && App._onVfoAck) App._onVfoAck(idx, obj);
                } else if (obj.type === "record_ack") {
                    if (typeof App !== 'undefined' && App._onRecordAck) App._onRecordAck(obj);
                } else if (obj.type === "auto_record_ack") {
                    if (typeof App !== 'undefined' && App._onAutoRecordAck) App._onAutoRecordAck(obj);
                } else if (obj.type === "auto_squelch_ack") {
                    if (typeof App !== 'undefined' && App._onAutoSquelchAck) App._onAutoSquelchAck(obj);
                } else if (obj.type === "squelch_ack") {
                    // Server confirmed this listener's per-listener squelch.
                    // Track it on the VFO and let App sync the slider from the
                    // acknowledged value (NOT the global status broadcast).
                    const v = this._vfos[idx];
                    if (v) v.squelch = obj.level;
                    if (typeof App !== 'undefined' && App._onSquelchAck) App._onSquelchAck(idx, obj);
                }
            } catch {
                // non-JSON (e.g. "pong") — ignore
            }
        };
        if (typeof raw === "string") handleText(raw);
        else if (raw && typeof raw.text === "function") raw.text().then(handleText);
    },

    // G.711 µ-law decode table (256 entries). Built lazily.
    _ulawTable: null,
    _buildUlawTable() {
        const t = new Int16Array(256);
        for (let i = 0; i < 256; i++) {
            let uval = ~i & 0xFF;
            const sign = (uval & 0x80) !== 0;
            let exponent = (uval >> 4) & 0x07;
            let mantissa = uval & 0x0F;
            let sample = ((mantissa << 3) + 0x84) << exponent;
            sample -= 0x84;
            t[i] = sign ? -sample : sample;
        }
        this._ulawTable = t;
        return t;
    },

    _handleFrame(idx, buffer) {
        const v = this._vfos[idx];
        if (!v || !this.ctx || !v.isPlaying) return;

        const view = new DataView(buffer);
        const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
        if (magic !== "AUD1" && magic !== "AUD2") {
            console.warn("Invalid audio frame magic:", magic);
            return;
        }
        const sampleRate = view.getUint32(4, true);
        const numSamples = view.getUint32(8, true);
        if (sampleRate !== v.sampleRate) v.sampleRate = sampleRate;

        const float32 = new Float32Array(numSamples);
        const offset = 12;
        if (magic === "AUD2") {
            const tbl = this._ulawTable || this._buildUlawTable();
            const bytes = new Uint8Array(buffer, offset, numSamples);
            for (let i = 0; i < numSamples; i++) float32[i] = tbl[bytes[i]] / 32768;
        } else {
            for (let i = 0; i < numSamples; i++) {
                float32[i] = view.getInt16(offset + i * 2, true) / 32768;
            }
        }

        const audioBuffer = this.ctx.createBuffer(1, numSamples, sampleRate);
        // Anti-click fade ramps at chunk edges.
        const rampN = Math.min(Math.floor(sampleRate * 0.002), numSamples >> 2);
        if (rampN > 1) {
            const ch0 = audioBuffer.getChannelData(0);
            for (let i = 0; i < rampN; i++) ch0[i] = float32[i] * (i / rampN);
            for (let i = 0; i < rampN; i++) {
                const j = numSamples - rampN + i;
                ch0[j] = float32[j] * ((rampN - i) / rampN);
            }
            ch0.set(float32.subarray(rampN, numSamples - rampN), rampN);
        } else {
            audioBuffer.copyToChannel(float32, 0);
        }

        this._scheduleBuffer(idx, audioBuffer);
        v.framesReceived++;
    },

    _scheduleBuffer(idx, audioBuffer) {
        const v = this._vfos[idx];
        if (!v) return;
        const now = this.ctx.currentTime;
        const targetLatency = 0.6;
        const maxLatency = 1.5;

        // RESYNC-ON-FLOOD: speed up slightly to drain backlog smoothly.
        const ahead = v.nextStartTime - now;
        let playbackRate = 1.0;
        if (v.nextStartTime !== 0 && ahead > maxLatency) {
            v.resyncs++;
            playbackRate = 1.08;
            const cap = now + maxLatency * 2;
            if (v.nextStartTime > cap) v.nextStartTime = cap;
        }
        // UNDERRUN recovery.
        if (v.nextStartTime === 0 || v.nextStartTime < now) {
            if (v.nextStartTime !== 0) v.underruns++;
            v.nextStartTime = now + targetLatency;
        }
        // Queue overflow guard.
        if (v.sourceNodes.length > 30) {
            const old = v.sourceNodes.shift();
            try { old.stop(); } catch (e) {}
            try { old.disconnect(); } catch (e) {}
        }

        const source = this.ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.playbackRate.value = playbackRate;
        // Route: source → muteNode (gain 0/1) → panner (L/R/both) → master →
        // speakers. Connect to muteNode so muting silences this VFO only.
        if (v.muteNode) source.connect(v.muteNode);
        else if (v.panner) source.connect(v.panner);
        else source.connect(this.masterGain);
        source.start(v.nextStartTime);
        v.nextStartTime += audioBuffer.duration / playbackRate;

        source.onended = () => {
            const i = v.sourceNodes.indexOf(source);
            if (i >= 0) v.sourceNodes.splice(i, 1);
        };
        v.sourceNodes.push(source);
    },

    // ------------------------------------------------------------------
    // UI
    // ------------------------------------------------------------------
    _updateUI() {
        const btn = document.getElementById("btn-listen");
        const indicator = document.getElementById("listen-indicator");
        const playing = this.anyPlaying;
        if (btn) {
            if (playing) { btn.textContent = I18n.t('dash.listening'); btn.classList.add("active"); }
            else { btn.textContent = I18n.t('dash.listenLive'); btn.classList.remove("active"); }
        }
        if (indicator) indicator.style.display = playing ? "" : "none";
        if (typeof App !== 'undefined' && App._updateVfoRows) App._updateVfoRows();
    },

    _updateStatus(icon) {
        const el = document.getElementById("audio-status");
        if (el) el.textContent = icon;
    },

    _startDebugOverlay() {
        if (this._debugTimer) clearInterval(this._debugTimer);
        this._debugTimer = setInterval(() => {
            if (!this.anyPlaying) { clearInterval(this._debugTimer); this._debugTimer = null; return; }
            const stats = this.getStats();
            const el = document.getElementById("audio-status");
            if (el) {
                el.textContent = `${stats.framesReceived}f | ${stats.underruns}u | ${stats.resyncs}r | ${stats.bufferAhead}`;
            }
        }, 1000);
    },

    /** Aggregate stats (active VFO primary). */
    getStats() {
        const v = this._activeVfo();
        const now = this.ctx ? this.ctx.currentTime : 0;
        const bufferAhead = v ? Math.max(0, v.nextStartTime - now) : 0;
        return {
            playing: this.anyPlaying,
            framesReceived: v ? v.framesReceived : 0,
            underruns: v ? v.underruns : 0,
            resyncs: v ? v.resyncs : 0,
            bufferAhead: bufferAhead.toFixed(3) + "s",
            activeNodes: v ? v.sourceNodes.length : 0,
            sampleRate: v ? v.sampleRate : 0,
        };
    },
};

// Re-render the listen button label when language changes.
document.addEventListener('languagechange', () => {
    if (typeof LiveAudio !== 'undefined') LiveAudio._updateUI();
});
