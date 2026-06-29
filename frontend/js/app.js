/**
 * Main Application - RTL-SDR Monitor Dashboard
 */
const App = {
    VERSION: '2026.06.26-v3',  // Waterfall AUTO (once/cont) fix: no longer
                               // clobbered by periodic status sync. AUTO
                               // values now persist until the user changes
                               // them or the active device changes.
                               // mute/speaker button to listen to ONLY that
                               // VFO (others auto-muted). Click again (or
                               // long-press the same VFO) to restore each
                               // VFO's previous mute state.
    currentFreq: 145500000,  // Hz
    currentMode: 'FM',
    currentSignal: -100,     // dBm
    bookmarks: [],
    recordings: [],
    isRecording: false,
    isConnected: false,
    isStreaming: false,
    _autoSquelchOn: false,

    // Waterfall state
    waterfallCanvas: null,
    waterfallCtx: null,
    waterfallHistory: [],
    waterfallMaxLines: 200,

    /**
     * Initialize the application
     */
    async init() {
        if (!Auth.requireAuth()) return;

        const user = Auth.getUser();
        if (user) {
            const userDisplay = document.getElementById('user-display');
            if (userDisplay) userDisplay.textContent = `👤 ${user.display_name || user.username}`;
        }

        // Hide admin-only UI elements for non-admin users.
        // This includes the Settings button and other controls marked .admin-only.
        if (!Auth.isAdmin()) {
            document.querySelectorAll('.admin-only').forEach(el => el.style.display = 'none');
        }

        this.startClock();
        this.setupModeSelector();
        this.setupRadioControls();
        this.initWaterfall();
        this.initThemeSwitch();

        // Sync state from backend first
        await this.syncState();

        WS.connect();
        this.setupWSListeners();

        // Send saved FFT size to backend
        const fftSize = parseInt(localStorage.getItem('fft_size') || '1024');
        API.sdr.setFFTSize(fftSize).catch(() => {});

        await this.loadRecentRecordings();
        this.initBookmarks();
        console.log('✅ App initialized v' + this.VERSION);
    },

    // Update AUTO button text/active based on _autoMode state
    // - off:  no class
    // - once: .active (solid blue)
    // - cont: .auto-active (green + blinking animation)
    _updateAutoBtn(btn) {
        if (!btn) return;
        const labels = {
            off:  '🎚️ AUTO',
            once: '🎚️ ONCE',
            cont: '🎚️ AUTO ●',
        };
        btn.textContent = labels[this._autoMode] || labels.off;
        btn.classList.toggle('active', this._autoMode === 'once');
        btn.classList.toggle('auto-active', this._autoMode === 'cont');
    },

    // === Theme (Classic / V2) ===
    initThemeSwitch() {
        // Apply saved theme (anti-flash script already does this before paint,
        // but this ensures correctness if that script is missing or JS runs late).
        const current = localStorage.getItem('ui_theme') || 'classic';
        if (current === 'v2') {
            document.documentElement.setAttribute('data-theme', 'v2');
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
        this._updateThemeButtons(current);
    },

    setTheme(name) {
        // name: 'classic' | 'v2'
        if (name !== 'classic' && name !== 'v2') return;
        localStorage.setItem('ui_theme', name);
        if (name === 'v2') {
            document.documentElement.setAttribute('data-theme', 'v2');
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
        this._updateThemeButtons(name);
        console.log(`🎨 Theme set to ${name}`);
    },

    _updateThemeButtons(active) {
        const classic = document.getElementById('theme-btn-classic');
        const v2 = document.getElementById('theme-btn-v2');
        if (classic) classic.classList.toggle('active', active === 'classic');
        if (v2) v2.classList.toggle('active', active === 'v2');
    },

    async syncState() {
        try {
            const status = await API.sdr.status();
            this.isConnected = status.connected || false;
            this.isStreaming = status.streaming || false;
            this.currentFreq = status.frequency || 145500000;
            this.currentMode = status.mode || 'FM';
            this.currentSignal = status.signal_level_db || -120;
            // Apply per-device waterfall dB range BEFORE syncing controls and
            // BEFORE WS.connect() (this is awaited in init()). This guarantees
            // _dbMin/_dbMax (and the _deviceWaterfallActive flag + color LUT
            // invalidation) are correct before the FIRST spectrum frame is
            // rendered — otherwise early frames use the hardcoded defaults
            // (-110/-55) and appear green before "snapping" to blue once the
            // first WS status broadcast arrives.
            this._applyDeviceWaterfallRange(status);
            // Sync sliders + SRATE button active state from backend reality
            this.syncControlsFromStatus(status);
            this.updateUI();
            console.log('🔄 State synced:', status);
        } catch (err) {
            console.warn('Sync failed:', err);
        }
    },

    updateUI() {
        const btnConnect = document.getElementById('btn-connect');
        const btnStream = document.getElementById('btn-stream');

        if (this.isConnected) {
            if (btnConnect) { btnConnect.style.display = 'none'; }
            if (btnStream) { btnStream.style.display = ''; }
        } else {
            if (btnConnect) { btnConnect.style.display = ''; btnConnect.textContent = I18n.t('dash.connect'); btnConnect.disabled = false; }
            if (btnStream) { btnStream.style.display = 'none'; }
        }

        if (this.isStreaming) {
            if (btnStream) { btnStream.textContent = I18n.t('dash.stopReceive'); }
        } else {
            if (btnStream) { btnStream.textContent = I18n.t('dash.receive'); }
        }

        this.updateFreqDisplay();
        this.updateSignalMeter(this.currentSignal);
        
        // Update mode selector
        document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === this.currentMode));
        const rm = document.getElementById('radio-mode');
        if (rm) rm.textContent = this.currentMode;
    },

    // Sync UI controls (sliders, selectors) from a status dict.
    // Called from syncState() and WS status updates.
    syncControlsFromStatus(data) {
        // Sync sample rate (BW display + freq-low/high + SRATE button active state)
        if (data.sample_rate !== undefined) {
            this._sampleRate = data.sample_rate;
            // Passband changed → keep the zoom view inside the new bounds.
            this._wfClampPan();
            this._waterfallDirty = true;
            const bw = document.getElementById('radio-bw');
            if (bw) {
                const sr = data.sample_rate;
                bw.textContent = sr >= 1e6 ? (sr / 1e6).toFixed(sr % 1e6 === 0 ? 0 : 3) + 'M' : (sr / 1e3).toFixed(0) + 'k';
            }
            // Highlight the SRATE button that matches the actual sample rate
            const srateButtons = document.getElementById('srate-buttons');
            if (srateButtons) {
                srateButtons.querySelectorAll('.step-btn').forEach(b =>
                    b.classList.toggle('active', parseInt(b.dataset.srate) === data.sample_rate)
                );
            }
            this.updateFreqDisplay();
        }
        // Sync channel bandwidth button active state (buttons in header).
        // While listening live, channel bandwidth is PER-LISTENER (sent via
        // the audio WS), so the global `data.channel_bw` is NOT this user's
        // value. Adopting it here would snap the active BW button back to the
        // global value every ~1s (the "เด้งกลับ" symptom). Only adopt it when
        // NOT listening live.
        if (data.channel_bw !== undefined) {
            const listeningBw = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
            if (!listeningBw) {
                this._channelBw = data.channel_bw;
                const bwButtons = document.getElementById('bw-buttons');
                if (bwButtons) {
                    bwButtons.querySelectorAll('.bw-btn').forEach(b =>
                        b.classList.toggle('active', parseInt(b.dataset.bw) === data.channel_bw)
                    );
                }
            }
        }
        // Sync gain slider + numeric input — but NOT while the user is editing
        // it (mousedown/focus/typing), or the periodic status broadcast would
        // snap the value back to the backend's stale value mid-drag.
        if (data.gain !== undefined && !this._activeEdits.has('gain')) {
            const gs = document.getElementById('gain-slider');
            const gi = document.getElementById('gain-input');
            if (gs) gs.value = data.gain;
            if (gi) gi.value = data.gain;
        }
        // Sync squelch slider + numeric input. While listening live the SQL is
        // PER-LISTENER (this user's own value, sent via the audio WS), so the
        // GLOBAL `data.squelch_level` is NOT this user's value. Adopting it
        // here would snap the slider back to the global value every ~1s (the
        // "เด้งกลับ" symptom that also let one user move another's slider). We
        // only adopt the global value when NOT listening live.
        if (data.squelch_level !== undefined && !this._activeEdits.has('squelch')) {
            const listeningSql = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
            if (!listeningSql) {
                const ss = document.getElementById('squelch-slider');
                const si = document.getElementById('squelch-input');
                if (ss) ss.value = data.squelch_level;
                if (si) si.value = data.squelch_level;
            }
        }
        // Sync PPM (Frequency Correction) slider + numeric input (same guard).
        if (data.freq_correction_ppm !== undefined && !this._activeEdits.has('ppm')) {
            const ps = document.getElementById('ppm-slider');
            const pi = document.getElementById('ppm-input');
            if (ps) {
                // Clamp display into slider range; actual value can exceed it via the input field.
                ps.value = Math.max(-100, Math.min(100, data.freq_correction_ppm));
            }
            if (pi) pi.value = data.freq_correction_ppm;
        }
        // VFO cursor is PER-USER. Only adopt the global value on the initial
        // sync (before the user has moved their own cursor). After that, each
        // client owns its cursor and is not dragged by other users' tunes.
        if (data.vfo_offset_hz !== undefined && !this._vfoUserTouched) {
            this._setVfoOffset(0, data.vfo_offset_hz);
            this.updateFreqDisplay();
        }
    },

    // === SDR Connection ===

    async connectSDR() {
        const btn = document.getElementById('btn-connect');
        btn.textContent = I18n.t('dash.connecting');
        btn.disabled = true;

        try {
            const result = await API.sdr.connect();
            this.isConnected = true;
            btn.textContent = I18n.t('dash.connected');
            btn.style.display = 'none';
            document.getElementById('btn-stream').style.display = '';
        } catch (err) {
            btn.textContent = I18n.t('dash.connect');
            btn.disabled = false;
            alert(I18n.t('dash.errConnectRtl') + (err.detail || err.message || err));
        }
    },

    async toggleStream() {
        const btn = document.getElementById('btn-stream');
        if (!btn) return;

        // Prevent double-clicks / race conditions while a request is in flight
        // (this was a contributor to the "flip back and forth" loop: clicking
        // again before the previous start/stop resolved).
        if (this._streamBusy) return;
        this._streamBusy = true;

        if (this.isStreaming) {
            // STOP
            const prevText = btn.textContent;
            btn.textContent = I18n.t('dash.stopping');
            btn.disabled = true;
            try {
                await API.sdr.streamStop();
                this.isStreaming = false;
                this.updateUI();
            } catch (e) {
                // Even on error, trust the WS status broadcast to correct us.
                console.error('Stream stop failed:', e);
                alert(I18n.t('dash.errStopStream') + (e.detail || e.message || e));
                this.updateUI();
            } finally {
                btn.disabled = false;
                this._streamBusy = false;
            }
        } else {
            // START — backend now auto-connects, so we no longer require the
            // user to press "ฟังสด" first.
            btn.textContent = I18n.t('dash.connecting');
            btn.disabled = true;
            try {
                await API.sdr.streamStart();
                // Only flip to "streaming" once the backend confirms success.
                // The old code flipped optimistically, so if the stream died a
                // moment later the button appeared to toggle on then off.
                this.isStreaming = true;
                this.isConnected = true;  // auto-connect succeeded → mark connected
                this.updateUI();
            } catch (err) {
                // Do NOT flip the button; show the reason from the backend.
                this.updateUI();
                alert(I18n.t('dash.errStartStream') + (err.detail || err.message || err));
            } finally {
                btn.disabled = false;
                this._streamBusy = false;
            }
        }
    },

    // === Clock ===
    startClock() {
        const months = I18n.tArr('date.months');
        const days = I18n.tArr('date.days');
        const update = () => {
            const now = new Date();
            const h = String(now.getHours()).padStart(2,'0');
            const m = String(now.getMinutes()).padStart(2,'0');
            const s = String(now.getSeconds()).padStart(2,'0');
            const ct = document.getElementById('clock-time');
            const cd = document.getElementById('clock-date');
            const cdy = document.getElementById('clock-day');
            if (ct) ct.textContent = `${h}:${m}:${s}`;
            const year = I18n.lang === 'th' ? now.getFullYear() + 543 : now.getFullYear();
            if (cd) cd.textContent = `${now.getDate()} ${months[now.getMonth()]} ${year}`;
            if (cdy) cdy.textContent = days[now.getDay()];
        };
        update(); setInterval(update, 1000);
    },

    // === Mode Selector ===
    setupModeSelector() {
        const selector = document.getElementById('mode-selector');
        if (!selector) return;
        selector.addEventListener('click', async (e) => {
            const btn = e.target.closest('.mode-btn');
            if (!btn) return;
            const mode = btn.dataset.mode;
            if (mode === this.currentMode) return;
            selector.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            this.currentMode = mode;
            const radioMode = document.getElementById('radio-mode');
            if (radioMode) radioMode.textContent = mode;
            // Per-listener mode change (audio WS) when listening; else global REST.
            if (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying && LiveAudio.setMode(mode)) {
                console.log(`🎛️ Mode (per-VFO): ${mode}`);
                return;
            }
            try { await API.sdr.setMode(mode); } catch (err) { console.error('Set mode failed:', err); }
        });
    },

    // === Radio Controls ===
    _stepSize: 1000,  // Default step: 1 kHz
    _sampleRate: 2048000,  // Default: 2.048 MSPS
    _channelBw: 12000,  // Default channel BW: 12 kHz (NBFM voice)
    // === Multi-VFO state (1-4 VFOs: A/B/C/D) ===
    // Each entry: { id, offsetHz }. Up to 4 independent listening cursors.
    // The number of ACTIVE VFOs is _vfoCount (1-4); only the first _vfoCount
    // slots drive a server-side listener + waterfall cursor. Inactive slots
    // are hidden from the UI.
    _MAX_VFOS: 4,
    _vfos: [
        { id: 'A', offsetHz: 0 },
        { id: 'B', offsetHz: 0 },
        { id: 'C', offsetHz: 0 },
        { id: 'D', offsetHz: 0 },
    ],
    _activeVfoIdx: 0,   // which VFO waterfall clicks / tune buttons target
    _vfoCount: 1,       // number of active VFOs (1-4)
    // Per-VFO identity + color (used for cursor, row button, freq label).
    // Keep in sync with .vfo-sel-X in app.css.
    _VFO_IDS: ['A', 'B', 'C', 'D'],
    // Waterfall cursor + freq-label color templates. '%a' = alpha slot.
    _VFO_COLORS: [
        'rgba(255, 80, 80, %a)',   // A red
        'rgba(80, 160, 255, %a)',  // B blue
        'rgba(80, 220, 120, %a)',  // C green
        'rgba(200, 120, 255, %a)', // D purple
    ],
    _VFO_LABEL_HEX: ['#ff5050', '#50a0ff', '#50dc78', '#c878ff'],
    _vfoId(idx) { return this._VFO_IDS[idx] || ('?' + idx); },
    _vfoLower(idx) { return (this._VFO_IDS[idx] || '?').toLowerCase(); },
    _vfoColorTpl(idx) { return this._VFO_COLORS[idx] || this._VFO_COLORS[0]; },
    _vfoLabelHex(idx) { return this._VFO_LABEL_HEX[idx] || this._VFO_LABEL_HEX[0]; },
    // Legacy alias kept so old WS/sync code (which still references
    // _vfoOffsetHz) reads/writes the ACTIVE VFO. New code should use
    // _vfoOffsetFor(idx) / _setVfoOffset(idx, hz).
    get _vfoOffsetHz() { return this._vfos[this._activeVfoIdx].offsetHz; },
    set _vfoOffsetHz(hz) { this._vfos[this._activeVfoIdx].offsetHz = hz; },
    /** Read a VFO's offset by index (used by audio.js to claim its cursor). */
    _vfoOffsetFor(idx) { return this._vfos[idx] ? this._vfos[idx].offsetHz : 0; },
    _setVfoOffset(idx, hz) { if (this._vfos[idx]) this._vfos[idx].offsetHz = hz; },
    // Becomes true the first time THIS user moves their own VFO cursor (click,
    // tune buttons, or freq input). After that the global VFO broadcast no
    // longer overwrites the local cursor — so one user tuning cannot drag
    // anyone else's cursor. Reset only on full page reload.
    _vfoUserTouched: false,
    // Per-VFO SIG levels (driven by the audio WS "sig" frames). Active VFO's
    // value feeds updateSignalMeter().
    _vfoSig: [-120, -120, -120, -120],
    // Per-VFO squelch-gate state (driven by sq_open in the audio WS "sig"
    // frames). True = audio is passing (signal > squelch) → the VFO row
    // flashes green (class "vfo-rx-active") so the operator can see which
    // frequency is active at a glance.
    _vfoSqOpen: [false, false, false, false],
    // Backwards-compat: code that checked _vfoMode === 'dual'.
    get _vfoMode() { return this._vfoCount > 1 ? 'dual' : 'single'; },
    // Set of control values the user is CURRENTLY editing (e.g. 'gain',
    // 'squelch', 'ppm'). While a control is in this set, syncControlsFromStatus
    // must NOT overwrite it from the periodic status broadcast — otherwise the
    // value the user is dragging/typing gets snapped back to the backend's
    // (stale) value every ~1s. A control enters the set on focus/mousedown and
    // leaves a short time after blur/release, so the backend-confirmed value is
    // re-adopted once the user is done.
    _activeEdits: new Set(),
    _activeEditTimers: {},

    /**
     * Mark a control as being actively edited so the periodic status sync
     * stops fighting the user. Re-armable: each call while editing pushes the
     * release out by `holdMs`, so a user dragging a slider keeps it locked
     * until they stop interacting.
     */
    _beginEdit(control, holdMs = 1500) {
        this._activeEdits.add(control);
        if (this._activeEditTimers[control]) clearTimeout(this._activeEditTimers[control]);
        this._activeEditTimers[control] = setTimeout(() => {
            this._activeEdits.delete(control);
            delete this._activeEditTimers[control];
        }, holdMs);
    },

    setupRadioControls() {
        // Role-based visibility:
        // - Tune buttons + freq display box: ALL users (used to tune the VFO /
        //   listening frequency within the passband).
        // - SRATE, GAIN, CENTER button: ADMIN ONLY (these change shared
        //   hardware settings that affect everyone on the SDR).
        const _user = Auth.getUser();
        if (_user && _user.role !== 'admin') {
            const adminOnly = ['srate-buttons', 'gain-slider-group', 'btn-center-freq', 'ppm-slider-group'];
            adminOnly.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.style.display = 'none';
            });
        }

        // Center frequency button (admin) → open dialog to set hardware center freq
        const centerFreqBtn = document.getElementById('btn-center-freq');
        if (centerFreqBtn) centerFreqBtn.addEventListener('click', () => this.showCenterFreqInput());

        // Fine tune (step size)
        const tuneDown = document.getElementById('btn-tune-down');
        if (tuneDown) tuneDown.addEventListener('click', () => this.tuneFreq(-this._stepSize));
        const tuneUp = document.getElementById('btn-tune-up');
        if (tuneUp) tuneUp.addEventListener('click', () => this.tuneFreq(this._stepSize));

        // Coarse tune (10x step)
        const tuneDownCoarse = document.getElementById('btn-tune-down-coarse');
        if (tuneDownCoarse) tuneDownCoarse.addEventListener('click', () => this.tuneFreq(-this._stepSize * 10));
        const tuneUpCoarse = document.getElementById('btn-tune-up-coarse');
        if (tuneUpCoarse) tuneUpCoarse.addEventListener('click', () => this.tuneFreq(this._stepSize * 10));

        // Step size selector
        const stepButtons = document.getElementById('step-buttons');
        if (stepButtons) {
            stepButtons.addEventListener('click', (e) => {
                const btn = e.target.closest('.step-btn');
                if (!btn) return;
                stepButtons.querySelectorAll('.step-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this._stepSize = parseInt(btn.dataset.step);
            });
        }

        // Sample rate selector
        const srateButtons = document.getElementById('srate-buttons');
        if (srateButtons) {
            srateButtons.addEventListener('click', (e) => {
                const btn = e.target.closest('.step-btn');
                if (!btn) return;
                const newRate = parseInt(btn.dataset.srate);
                srateButtons.querySelectorAll('.step-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.setSampleRate(newRate);
            });
        }

        // Channel bandwidth selector (buttons in header)
        const bwButtons = document.getElementById('bw-buttons');
        if (bwButtons) {
            bwButtons.addEventListener('click', async (e) => {
                const btn = e.target.closest('.bw-btn');
                if (!btn) return;
                const newBw = parseInt(btn.dataset.bw);
                bwButtons.querySelectorAll('.bw-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this._channelBw = newBw;
                // Per-VFO bandwidth change (audio WS) when listening; else global REST.
                if (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying && LiveAudio.setBandwidth(newBw)) {
                    console.log(`🎛️ Channel BW (per-VFO): ${newBw/1e3} kHz`);
                    return;
                }
                try {
                    await API.sdr.setBandwidth(newBw);
                    console.log(`🎛️ Channel BW set to ${newBw/1e3} kHz`);
                } catch (err) {
                    console.error('Set bandwidth failed:', err);
                }
            });
        }

        // Panel-right collapse toggle (for small screens / tablets)
        const collapseBtn = document.getElementById('panel-collapse-btn');
        const panelRight = document.getElementById('panel-right');
        if (collapseBtn && panelRight) {
            // Restore saved state
            if (localStorage.getItem('panel_right_collapsed') === 'true') {
                panelRight.classList.add('collapsed');
                collapseBtn.textContent = '⬇';
                collapseBtn.classList.add('is-collapsed');
            }
            collapseBtn.addEventListener('click', () => {
                const isCollapsed = panelRight.classList.toggle('collapsed');
                // Mark as user-expanded so mobile media query doesn't auto-hide
                panelRight.classList.add('user-expanded');
                collapseBtn.textContent = isCollapsed ? '⬇' : '⬆';
                collapseBtn.classList.toggle('is-collapsed', isCollapsed);
                localStorage.setItem('panel_right_collapsed', String(isCollapsed));
            });
        }

        // Waterfall Auto Range button
        // - CLICK (release < 500ms): ONCE — compute once then back to OFF
        // - HOLD (≥ 500ms): toggle CONT (continuous, button blinks)
        // - OFF is the default on page load / refresh (not persisted)
        const wfAutoBtn = document.getElementById('wf-auto-btn');
        if (wfAutoBtn) {
            this._autoMode = 'off';  // always start OFF on page load
            this._updateAutoBtn(wfAutoBtn);

            let holdTimer = null;
            let didHold = false;

            const startHold = () => {
                didHold = false;
                holdTimer = setTimeout(() => {
                    didHold = true;
                    // Toggle continuous mode
                    this._autoMode = (this._autoMode === 'cont') ? 'off' : 'cont';
                    if (this._autoMode === 'cont') {
                        this._autoContMin = this.waterfallDbMin;
                        this._autoContMax = this.waterfallDbMax;
                        console.log('🎚️ Waterfall AUTO = CONT (continuous tracking)');
                    } else {
                        console.log('🎚️ Waterfall AUTO = OFF (stopped continuous)');
                    }
                    this._updateAutoBtn(wfAutoBtn);
                }, 500);
            };

            const endHold = () => {
                if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
                if (!didHold) {
                    // Quick click → ONCE
                    // Show ONCE label, compute on next spectrum frame,
                    // then keep showing computed values for ~0.6s before
                    // reverting to OFF so user can read the result.
                    this._autoMode = 'once';
                    this._onceComputed = false;
                    this._updateAutoBtn(wfAutoBtn);
                    console.log('🎚️ Waterfall AUTO = ONCE (will compute on next frame)');
                    if (this._onceTimer) clearTimeout(this._onceTimer);
                    this._onceTimer = setTimeout(() => {
                        this._autoMode = 'off';
                        this._updateAutoBtn(wfAutoBtn);
                        this._onceTimer = null;
                    }, 600);
                }
            };

            // Mouse events
            wfAutoBtn.addEventListener('mousedown', startHold);
            wfAutoBtn.addEventListener('mouseup', endHold);
            wfAutoBtn.addEventListener('mouseleave', () => {
                if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
            });

            // Touch events (mobile)
            wfAutoBtn.addEventListener('touchstart', (e) => { e.preventDefault(); startHold(); }, { passive: false });
            wfAutoBtn.addEventListener('touchend', (e) => { e.preventDefault(); endHold(); }, { passive: false });
        }

        // Signal Tag toggle — show/hide frequency annotations on strong signals
        const sigtagBtn = document.getElementById('sigtag-btn');
        if (sigtagBtn) {
            const syncSigtagBtn = () => {
                const on = this.signalTagEnabled;
                sigtagBtn.classList.toggle('active', on);
                sigtagBtn.textContent = on ? '📡 TAG' : '📡 OFF';
            };
            syncSigtagBtn();
            sigtagBtn.addEventListener('click', () => {
                this.signalTagEnabled = !this.signalTagEnabled;
                syncSigtagBtn();
                if (!this.signalTagEnabled) {
                    this._signalAnnotations = [];
                    this._sigTagLastShown = {};
                    this._waterfallDirty = true;
                }
                console.log(`📡 Signal Tag ${this.signalTagEnabled ? 'ON' : 'OFF'}`);
            });
        }

        // Waterfall 3D toggle — switches between the 2D scroll-blit waterfall
        // and the Three.js 3D terrain view. Lazily inits WebGL on first enable.
        const wf3dBtn = document.getElementById('wf-3d-btn');
        if (wf3dBtn) {
            // Disable the button if Three.js failed to load (offline CDN).
            if (typeof THREE === 'undefined') {
                wf3dBtn.disabled = true;
                wf3dBtn.title = I18n.t('dash.errThreejs');
                wf3dBtn.style.opacity = '0.4';
            } else {
                wf3dBtn.addEventListener('click', () => this.toggleWaterfall3D());
            }
        }

        // Direct frequency input (click on freq display)
        const freqBox = document.getElementById('freq-display-box');
        if (freqBox) freqBox.addEventListener('click', () => this.showFreqInput());

        // Helper: keep slider ↔ numeric input in sync bidirectionally and
        // send the value to the backend (with optional debounce).
        const _bindSliderInputPair = ({ sliderId, inputId, setter, debounceMs = 0, clampToSliderRange = false, controlName = null }) => {
            const slider = document.getElementById(sliderId);
            const input = document.getElementById(inputId);
            if (!slider || !input) return;
            // controlName lets us lock this control against status-broadcast
            // overwrites while the user is editing it. Defaults to sliderId.
            const ctrl = controlName || sliderId.replace('-slider', '');
            let syncGuard = false;  // prevent feedback loops slider↔input
            let timer = null;
            const send = (val) => {
                if (timer) clearTimeout(timer);
                const doSend = () => {
                    try { setter(val); } catch (err) { console.error('Set failed:', err); }
                };
                if (debounceMs > 0) timer = setTimeout(doSend, debounceMs);
                else doSend();
            };
            // Any direct interaction claims the control so the periodic status
            // sync stops snapping its value back while the user is mid-edit.
            const claim = () => this._beginEdit(ctrl);
            slider.addEventListener('mousedown', claim);
            slider.addEventListener('touchstart', claim, { passive: true });
            input.addEventListener('focus', claim);
            // Slider → input
            slider.addEventListener('input', (e) => {
                claim();  // keep locked while dragging
                if (syncGuard) return;
                syncGuard = true;
                input.value = e.target.value;
                syncGuard = false;
            });
            slider.addEventListener('change', (e) => {
                send(parseInt(e.target.value));
            });
            // Input → slider
            input.addEventListener('input', (e) => {
                claim();  // keep locked while typing
                if (syncGuard) return;
                syncGuard = true;
                let v = parseInt(e.target.value);
                if (!isNaN(v)) {
                    // Slider only shows a limited range; clamp the displayed
                    // slider position but keep the actual value in the input.
                    if (clampToSliderRange) {
                        slider.value = Math.max(parseInt(slider.min), Math.min(parseInt(slider.max), v));
                    } else {
                        slider.value = v;
                    }
                }
                syncGuard = false;
            });
            input.addEventListener('change', (e) => {
                const v = parseInt(e.target.value);
                if (!isNaN(v)) send(v);
            });
        };

        // Gain (slider + numeric input)
        _bindSliderInputPair({
            sliderId: 'gain-slider',
            inputId: 'gain-input',
            setter: (v) => API.sdr.setGain(v),
        });

        // Squelch (slider + numeric input) — PER-LISTENER (per-user), like a
        // real radio. Each user's SQL is independent:
        //   - Listening live → send ONLY to this user's per-listener audio WS
        //     ({"type":"squelch"}). Never touches the global state, so one user
        //     moving SQL no longer moves anyone else's slider.
        //   - Not listening → remember the value locally (_pendingSquelch) and
        //     push it to the listener as soon as they press ฟังสด. Admins may
        //     additionally set the GLOBAL baseline via the admin-only
        //     /api/sdr/squelch endpoint (used for global auto-record gating +
        //     the initial value handed to new listeners).
        //   Values <= -120 disable squelch (pass everything, including noise).
        _bindSliderInputPair({
            sliderId: 'squelch-slider',
            inputId: 'squelch-input',
            setter: (v) => {
                const level = parseFloat(v);
                // Remember this user's chosen SQL so it applies on next listen.
                this._pendingSquelch = level;
                if (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying) {
                    // Per-listener (broadcast to all of THIS user's VFOs only).
                    LiveAudio.setSquelch(level);
                } else if (Auth.isAdmin && Auth.isAdmin()) {
                    // Admin (not listening) may also set the global baseline.
                    API.sdr.setSquelch(level);
                }
            },
        });

        // Auto Squelch toggle: when ON, the server tracks the noise floor and
        // gates audio automatically; the manual SQL slider is disabled until
        // the user turns AUTO off again.
        const sqlAutoBtn = document.getElementById('sql-auto-btn');
        if (sqlAutoBtn) {
            sqlAutoBtn.addEventListener('click', () => {
                if (typeof LiveAudio === 'undefined' || !LiveAudio.anyPlaying) {
                    this._onAutoSquelchAck({ enabled: false });
                    alert(I18n.t('dash.errNeedListenAutoSql'));
                    return;
                }
                const next = !this._autoSquelchOn;
                LiveAudio.setAutoSquelch(next);
                // UI updates on auto_squelch_ack from the server (below).
            });
        }

        // PPM / Frequency Correction (slider + numeric input, debounced)
        // Numeric input allows values beyond the slider range (-200..200 vs -100..100).
        _bindSliderInputPair({
            sliderId: 'ppm-slider',
            inputId: 'ppm-input',
            setter: (v) => API.sdr.setFreqCorrection(v),
            debounceMs: 300,
            clampToSliderRange: true,
        });

        // Mini +/- buttons: adjust Gain / SQL / PPM by ±1 (data-target maps
        // to the input/slider ID prefix, e.g. "gain" → "gain-input").
        document.querySelectorAll('.step-mini-btn[data-target]').forEach(btn => {
            btn.addEventListener('click', () => {
                const target = btn.dataset.target;       // gain | squelch | ppm
                const delta = parseInt(btn.dataset.delta);  // +1 or -1
                const input = document.getElementById(target + '-input');
                const slider = document.getElementById(target + '-slider');
                if (!input) return;
                let v = parseInt(input.value) || 0;
                v += delta;
                // Clamp to the numeric input's min/max (the authoritative range)
                const min = parseInt(input.min);
                const max = parseInt(input.max);
                if (!isNaN(min)) v = Math.max(min, v);
                if (!isNaN(max)) v = Math.min(max, v);
                input.value = v;
                if (slider) {
                    const sMin = parseInt(slider.min);
                    const sMax = parseInt(slider.max);
                    slider.value = Math.max(sMin, Math.min(sMax, v));
                }
                // Dispatch change so the bound pair handler sends it to backend
                input.dispatchEvent(new Event('change', { bubbles: true }));
            });
        });

        // Record toggle button
        const btnRecord = document.getElementById('btn-record');
        if (btnRecord) btnRecord.addEventListener('click', () => this.toggleRecording());

        // Per-user AUTO-record toggle button (live listening only). When ON,
        // the server auto-records the channel THIS user is listening to, gated
        // by the user's own squelch + signal (independent of everyone else and
        // of the global auto-record). Disabled until the user presses ฟังสด.
        const btnAutoRec = document.getElementById('btn-auto-record');
        if (btnAutoRec) btnAutoRec.addEventListener('click', () => this.toggleAutoRecord());

        // === Multi-VFO controls (1-4 VFOs: A/B/C/D) ===
        // +VFO / -VFO count buttons. NOTE: these now live INSIDE
        // #freq-display-box, whose own click handler opens the freq-input
        // modal. Stop propagation so clicking +/- only adjusts the count
        // (doesn't also pop the modal).
        const btnVfoAdd = document.getElementById('btn-vfo-add');
        if (btnVfoAdd) btnVfoAdd.addEventListener('click', (e) => { e.stopPropagation(); this.adjustVfoCount(+1); });
        const btnVfoSub = document.getElementById('btn-vfo-sub');
        if (btnVfoSub) btnVfoSub.addEventListener('click', (e) => { e.stopPropagation(); this.adjustVfoCount(-1); });
        // Per-VFO select / speaker / mute — wired by index so all 4 rows work.
        for (let i = 0; i < this._MAX_VFOS; i++) {
            const lo = this._vfoLower(i);
            const sel = document.getElementById(`btn-vfo-${lo}`);
            if (sel) sel.addEventListener('click', () => this.selectActiveVfo(i));
            const sp = document.getElementById(`vfo-${lo}-speaker`);
            if (sp) sp.addEventListener('change', (e) => this.setVfoSpeaker(i, e.target.value));
            const mu = document.getElementById(`vfo-${lo}-mute`);
            if (mu) this._bindVfoMuteButton(mu, i);
        }
        // Initial state.
        this._updateVfoCountBtn();
        this._updateVfoRows();

        // Volume slider
        this.initVolumeSlider();

        // Sync recording state from backend on init
        this._syncRecordingState();

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (e.key === 'ArrowLeft') { e.preventDefault(); this.tuneFreq(-this._stepSize); }
            if (e.key === 'ArrowRight') { e.preventDefault(); this.tuneFreq(this._stepSize); }
            if (e.key === 'ArrowUp') { e.preventDefault(); this.tuneFreq(this._stepSize * 10); }
            if (e.key === 'ArrowDown') { e.preventDefault(); this.tuneFreq(-this._stepSize * 10); }
        });
    },

    setSampleRate(rate) {
        this._sampleRate = rate;
        // Sample-rate change alters the passband width → re-clamp the zoom view.
        this._wfClampPan();
        this._waterfallDirty = true;
        // Update BW display
        const bw = document.getElementById('radio-bw');
        if (bw) {
            bw.textContent = rate >= 1e6 ? (rate/1e6).toFixed(3)+'M' : (rate/1e3).toFixed(0)+'k';
        }
        // Update freq-low / freq-high
        this.updateFreqDisplay();
        // Send to backend
        API.sdr.setSampleRate(rate).catch(err => console.error('Set sample rate failed:', err));
    },

    _onWaterfallClick(e) {
        const canvas = this.waterfallCanvas;
        if (!canvas) return;
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const w = rect.width;
        // Map the click through the current zoom/pan view.
        const freq = Math.round(this._wfXToFreq(x, w));
        this.setVFO(freq);
    },

    async setVFO(freq) {
        // VFO tuning is PER-USER and now also PER-VFO (A/B). It NEVER
        // broadcasts to other users. The active VFO (this._activeVfoIdx) is
        // the target of clicks / tune buttons / freq input.
        //   - Listening live → send via the ACTIVE VFO's audio WebSocket so
        //     THAT listener's demodulator tunes (audio follows immediately).
        //   - Not listening  → move ONLY this browser's cursor (client-side).
        this._vfoUserTouched = true;
        const idx = this._activeVfoIdx;
        if (typeof LiveAudio !== 'undefined' && LiveAudio.isVfoPlaying(idx) && LiveAudio.setVfoVfo(idx, freq)) {
            const offset = freq - this.currentFreq;
            this._setVfoOffset(idx, offset);
            this._waterfallDirty = true;
            this.updateFreqDisplay();
            this._updateVfoRows();  // refresh the row's freq label
            console.log(`🎯 VFO ${this._vfos[idx].id} (per-listener): ${(freq/1e6).toFixed(4)} MHz`);
            return;
        }
        // Client-side only: keep this user's cursor within its passband.
        const sr = this._sampleRate || 0;
        let offset = freq - this.currentFreq;
        if (sr > 0) {
            const maxOff = sr * 0.5 * 0.95;
            offset = Math.max(-maxOff, Math.min(maxOff, offset));
        }
        this._setVfoOffset(idx, offset);
        this._waterfallDirty = true;
        this.updateFreqDisplay();
        this._updateVfoRows();  // refresh the row's freq label
        // Not listening live: tell the server our cursor offset so it can
        // compute the SIG under our cursor (not just the global center) and
        // send it back in the next status frame → VFO A bar reflects the freq
        // we clicked. Only meaningful for VFO A in single mode (the bar we
        // drive when not live). Live mode uses per-listener 'sig' frames.
        if (idx === 0 && typeof WS !== 'undefined') {
            WS.send({ type: "vfo_cursor", offset_hz: offset });
        }
        console.log(`🎯 VFO ${this._vfos[idx].id} (local cursor): ${(freq/1e6).toFixed(4)} MHz`);
    },

    tuneFreq(delta) {
        // All users (admin + user) move the VFO (listening freq) within the
        // passband. The hardware center frequency is set separately by an
        // admin via the CENTER button (showCenterFreqInput).
        const vfoFreq = this.currentFreq + (this._vfoOffsetHz || 0) + delta;
        this.setVFO(vfoFreq);
    },

    // Shared core for the frequency input dialog (used by VFO + center freq).
    // RTL-SDR Blog V4 covers HF (via direct sampling, ~500 kHz up) through
    // ~1766 MHz, so allow down to 0.5 MHz. The VFO dialog passes a tighter
    // floor only when it makes sense (it doesn't here — VFO follows whatever
    // center the admin set, which may itself be HF).
    _openFreqInputDialog({ title, defaultValueMHz, onApply, minMHz = 0.5, maxMHz = 1766 }) {
        const overlay = document.createElement('div');
        overlay.className = 'freq-input-overlay';
        overlay.innerHTML = `
            <div class="freq-input-box">
                <h3>${title}</h3>
                <input type="text" id="freq-input-field" value="${defaultValueMHz}" autofocus>
                <div class="freq-input-btns">
                    <button class="btn btn-primary" id="freq-input-ok">${I18n.t('dash.setBtn')}</button>
                    <button class="btn" id="freq-input-cancel">${I18n.t('dash.cancel')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const input = document.getElementById('freq-input-field');
        input.focus();
        input.select();

        const close = () => overlay.remove();
        const apply = () => {
            const val = parseFloat(input.value);
            if (!isNaN(val) && val >= minMHz && val <= maxMHz) {
                onApply(Math.round(val * 1e6));
            } else {
                alert(I18n.t('dash.errFreqRange', minMHz, maxMHz));
                return;  // keep dialog open so the user can correct it
            }
            close();
        };

        document.getElementById('freq-input-ok').addEventListener('click', apply);
        document.getElementById('freq-input-cancel').addEventListener('click', close);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') apply();
            if (e.key === 'Escape') close();
        });
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    },

    // VFO (listening) frequency input — available to ALL users.
    // Opens when clicking the big frequency display box.
    showFreqInput() {
        const vfoHz = this.currentFreq + (this._vfoOffsetHz || 0);
        const currentMHz = (vfoHz / 1e6).toFixed(4);
        this._openFreqInputDialog({
            title: I18n.t('dash.freqInputTitleVfo'),
            defaultValueMHz: currentMHz,
            onApply: (freqHz) => this.setVFO(freqHz),
        });
    },

    // Center frequency input — ADMIN ONLY.
    // Moves the hardware center frequency (the whole waterfall shifts for
    // everyone watching). Triggered by the CENTER button.
    showCenterFreqInput() {
        const currentMHz = (this.currentFreq / 1e6).toFixed(4);
        this._openFreqInputDialog({
            title: I18n.t('dash.freqInputTitleCenter'),
            defaultValueMHz: currentMHz,
            onApply: (freqHz) => {
                this.currentFreq = freqHz;
                this._vfoOffsetHz = 0;  // VFO resets when center changes
                this.updateFreqDisplay();
                API.sdr.setFrequency(freqHz).catch(err => console.error('Center tune failed:', err));
            },
        });
    },

    updateFreqDisplay() {
        // === Shift waterfall back buffer when CENTER frequency changes ===
        // This makes existing waterfall lines scroll horizontally, so you
        // can see where signals are relative to the new center frequency.
        // (VFO offset changes do NOT shift the buffer — only center does.)
        if (this._waterfallBackBuffer && this._lastWaterfallFreq !== undefined) {
            const deltaHz = this.currentFreq - this._lastWaterfallFreq;
            if (Math.abs(deltaHz) > 0) {
                this._shiftWaterfallBuffer(deltaHz);
            }
        }
        this._lastWaterfallFreq = this.currentFreq;

        // === Big display box: show the ACTIVE VFO (listening) frequency ===
        const freqDisplay = document.getElementById('freq-display');
        if (freqDisplay) {
            const vfoHz = this.currentFreq + (this._vfoOffsetHz || 0);
            const mhzVfo = vfoHz / 1e6;
            const parts = mhzVfo.toFixed(4).split('.');
            const intPart = parts[0].split('').join(' ');
            const decPart = parts[1];
            // Warn when the VFO is outside the usable passband (the backend
            // should already have reset/clamped it, but if it ever slips we
            // want the user to see WHY the audio is noise).
            const offscreen = !this._isVfoInPassband();
            const warn = offscreen
                ? ` <span style="color:#ff5050;font-size:0.5em;vertical-align:middle">${I18n.t('dash.outOfRange')}</span>`
                : '';
            // When more than one VFO is active, prefix the big display with the
            // active VFO label + its color so the operator knows which cursor
            // the box reflects.
            const vfoLabel = (this._vfoCount > 1)
                ? `<span style="color:${this._vfoLabelHex(this._activeVfoIdx)};font-size:0.45em;vertical-align:middle;margin-right:6px">VFO ${this._vfos[this._activeVfoIdx].id}</span>`
                : '';
            // Show all 4 decimal digits
            freqDisplay.innerHTML = `${vfoLabel}${intPart} . ${decPart.slice(0,3)} <span class="decimal">${decPart.slice(3)}</span>${warn}`;
        }

        // === Waterfall overlay: show CENTER frequency + band edges ===
        const mhz = this.currentFreq / 1e6;
        const freqCenter = document.getElementById('freq-center');
        if (freqCenter) freqCenter.textContent = `${mhz.toFixed(4)} MHz`;

        // Compute freq-low / freq-high from the *visible* window (respects zoom/pan)
        const lowMhz = this._wfViewLow() / 1e6;
        const highMhz = this._wfViewHigh() / 1e6;
        const freqLow = document.getElementById('freq-low');
        const freqHigh = document.getElementById('freq-high');
        if (freqLow) freqLow.textContent = `${lowMhz.toFixed(4)} MHz`;
        if (freqHigh) freqHigh.textContent = `${highMhz.toFixed(4)} MHz`;
    },

    async toggleRecording() {
        const btn = document.getElementById('btn-record');
        // Manual recording is now PER-USER via the live audio WebSocket.
        // Each user records their OWN audio (at their VFO/mode/bandwidth),
        // independent of everyone else.
        // Guard: require live audio to be active (the WS carries the recorder)
        if (!this.isRecording && !LiveAudio.isPlaying) {
            alert(I18n.t('dash.errNeedListenRecord'));
            return;
        }
        if (this.isRecording) {
            // Stop → server saves the file and replies with record_ack
            LiveAudio.stopRecording();
            // State is finalized in _onRecordAck (below)
        } else {
            // Start → server checks recording-enabled toggle for role=user
            LiveAudio.startRecording();
            // Optimistically show recording state; _onRecordAck will correct
            // it if the server rejects (e.g. recording disabled).
            this.isRecording = true;
            if (btn) btn.classList.add('recording');
            console.log('⏺ Per-user manual recording requested');
        }
    },

    /**
     * Handle record_ack messages from the audio WebSocket.
     * Possible statuses: recording | already_recording | saved | no_audio | disabled
     */
    _onRecordAck(obj) {
        const btn = document.getElementById('btn-record');
        switch (obj.status) {
            case 'recording':
                this.isRecording = true;
                if (btn) btn.classList.add('recording');
                console.log('⏺ Server confirmed recording started');
                break;
            case 'already_recording':
                this.isRecording = true;
                if (btn) btn.classList.add('recording');
                break;
            case 'saved':
                this.isRecording = false;
                if (btn) btn.classList.remove('recording');
                console.log('⏹ Recording saved:', obj.recording);
                break;
            case 'no_audio':
                this.isRecording = false;
                if (btn) btn.classList.remove('recording');
                console.log('⏹ Recording stopped (no audio captured)');
                break;
            case 'disabled':
                // The admin has disabled manual recording for regular users.
                this.isRecording = false;
                if (btn) btn.classList.remove('recording');
                alert(obj.message || I18n.t('dash.recordingDisabled'));
                break;
            default:
                console.warn('🎙️ Unknown record_ack status:', obj.status);
        }
    },

    /**
     * Toggle per-user AUTO-record (live listening only). Each user can
     * independently auto-record the channel they are listening to, gated by
     * their own squelch threshold + measured signal. Uses the current SQL
     * slider value as the record gate; <= -120 means "always record".
     */
    _autoRecordOn: false,
    toggleAutoRecord() {
        // Auto-record (per-user) rides on the live audio WebSocket, so it
        // requires "ฟังสด" to be active first.
        if (typeof LiveAudio === 'undefined' || !LiveAudio.anyPlaying) {
            alert(I18n.t('dash.errNeedListenAutoRec'));
            return;
        }
        const next = !this._autoRecordOn;
        // Use the current SQL slider value as the record-gate threshold so the
        // user's existing squelch setting controls what gets recorded. This is
        // separate from the live LISTENING squelch on the server.
        const sqlInput = document.getElementById('squelch-input');
        const squelch = sqlInput ? parseFloat(sqlInput.value) : -120;
        LiveAudio.setAutoRecord(next, squelch);
        // UI state is finalized in _onAutoRecordAck (server confirms).
    },

    /**
     * Handle auto_record_ack messages from the audio WebSocket.
     * Updates the AUTO-REC button active state.
     */
    _onAutoRecordAck(obj) {
        this._autoRecordOn = !!obj.enabled;
        const btn = document.getElementById('btn-auto-record');
        if (btn) {
            btn.classList.toggle('auto-rec-active', this._autoRecordOn);
            btn.textContent = this._autoRecordOn ? '🔄●' : '🔄';
            btn.title = this._autoRecordOn
                ? I18n.t('dash.autoRecOnTitle')
                : I18n.t('dash.autoRecOffTitle');
        }
        if (obj.message) {
            // e.g. recording disabled by admin → revert and inform.
            alert(obj.message);
        }
        console.log(`🔄 Per-user auto-record → ${this._autoRecordOn ? 'ON' : 'OFF'}`);
    },

    // === Per-VFO callbacks (driven by LiveAudio's per-VFO audio WS) ===

    /** A VFO's listener became "ready" on the server. */
    _onVfoReady(idx, obj) {
        // Adopt the server's per-listener default offset only if THIS VFO's
        // cursor hasn't been user-touched yet.
        if (obj.vfo_offset_hz !== undefined && this._vfos[idx]) {
            this._setVfoOffset(idx, obj.vfo_offset_hz);
            this.updateFreqDisplay();
        }
    },

    /** A VFO's per-listener SIG level update (its own channel power).
     *  Also carries `sq_open` (squelch gate open = audio is passing) which
     *  flashes the VFO row green so the operator can see which frequency is
     *  active at a glance. */
    _onVfoSig(idx, db, sqOpen) {
        if (idx >= 0 && idx < this._vfoSig.length) this._vfoSig[idx] = db;
        if (idx >= 0 && idx < this._vfoSqOpen.length) this._vfoSqOpen[idx] = !!sqOpen;
        // Refresh the SIG bar. In dual mode both VFOs feed separate bars.
        this._renderVfoSig();
        // Apply/remove the green "RX active" flash on the VFO row.
        this._updateVfoRxActive(idx);
    },

    /** Toggle the "vfo-rx-active" class on a VFO row based on its squelch
     *  gate state. When audio is passing (sq_open=true) the row flashes
     *  green via the CSS animation. Only flashes when the VFO is actually
     *  live (a stale sq_open from a closed WS shouldn't keep a row flashing). */
    _updateVfoRxActive(idx) {
        const lo = this._vfoLower(idx);
        const row = document.getElementById(`vfo-row-${lo}`);
        if (!row) return;
        const open = !!(this._vfoSqOpen && this._vfoSqOpen[idx]);
        const live = (typeof LiveAudio !== 'undefined') ? LiveAudio.isVfoPlaying(idx) : false;
        row.classList.toggle('vfo-rx-active', open && live);
    },

    /** A VFO's vfo_ack (server confirmed this VFO's tune). */
    _onVfoAck(idx, obj) {
        if (this._vfos[idx] && obj.vfo_offset_hz !== undefined) {
            this._setVfoOffset(idx, obj.vfo_offset_hz);
            this.updateFreqDisplay();
            this._updateVfoRows();
        }
    },

    /**
     * Render the SIG meters for all active VFOs (each row has its own bar).
     * Also keeps currentSignal in sync with the strongest active VFO.
     */
    _renderVfoSig() {
        const listening = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
        if (!listening) return;  // global SIG drives the bar when not listening
        let strongest = -120;
        for (let i = 0; i < this._vfoCount; i++) {
            const s = this._vfoSig[i];
            const val = (s === undefined) ? -120 : s;
            this._updateVfoBars(i, val);
            // Keep the RX-active flash in sync with the latest SIG data.
            this._updateVfoRxActive(i);
            if (val > strongest) strongest = val;
        }
        this.updateSignalMeter(strongest);
    },

    /** Light the compact SIG bars in VFO row `idx` according to `db`. */
    _updateVfoBars(idx, db) {
        const lo = this._vfoLower(idx);
        const container = document.getElementById(`vfo-${lo}-sig-bars`);
        const label = document.getElementById(`vfo-${lo}-sig`);
        if (!container) return;
        const v = (db === undefined) ? -120 : db;
        const bars = container.querySelectorAll('.vfo-bar');
        const normalized = Math.max(0, Math.min(10, (v + 120) / 9));
        const activeBars = Math.round(normalized);
        bars.forEach((bar, i) => {
            bar.className = 'vfo-bar';
            if (i < activeBars) {
                if (i < 5) bar.classList.add('active-green');
                else if (i < 8) bar.classList.add('active-yellow');
                else bar.classList.add('active-red');
            }
        });
        if (label) label.textContent = `${v.toFixed(0)} dB`;
    },

    /**
     * Refresh the VFO control rows (frequencies, active highlight, speaker
     * dropdown, live indicator, mute). Called by LiveAudio._updateUI and on
     * count/active changes. Generalized to loop 0..MAX_VFOS-1.
     */
    _updateVfoRows() {
        const cf = this.currentFreq || 0;
        for (let i = 0; i < this._MAX_VFOS; i++) {
            const lo = this._vfoLower(i);
            const row = document.getElementById(`vfo-row-${lo}`);
            if (!row) continue;
            // Show only active VFOs (first _vfoCount slots).
            row.style.display = (i < this._vfoCount) ? '' : 'none';
            row.classList.toggle('vfo-active', i === this._activeVfoIdx);
            // Frequency label.
            const f = document.getElementById(`vfo-${lo}-freq`);
            if (f) f.textContent = ((cf + (this._vfos[i].offsetHz || 0)) / 1e6).toFixed(4) + ' MHz';
            // Live indicator.
            const live = document.getElementById(`vfo-${lo}-live`);
            if (live) live.textContent = (typeof LiveAudio !== 'undefined' && LiveAudio.isVfoPlaying(i)) ? '🔴' : '';
            // Speaker dropdown.
            const sp = document.getElementById(`vfo-${lo}-speaker`);
            if (sp && typeof LiveAudio !== 'undefined') sp.value = LiveAudio.getSpeaker(i);
            // Mute button. Reflects SOLO state first (priority), else mute.
            const m = document.getElementById(`vfo-${lo}-mute`);
            if (m && typeof LiveAudio !== 'undefined') {
                const solo = LiveAudio.isSolo(i);
                const anySolo = LiveAudio.anySolo;
                const muted = LiveAudio.isMuted(i);
                if (solo) {
                    m.textContent = '🎧';
                    m.classList.add('vfo-solo');
                    m.classList.remove('muted');
                } else {
                    m.classList.remove('vfo-solo');
                    m.textContent = (anySolo || muted) ? '🔇' : '🔊';
                    m.classList.toggle('muted', muted);
                }
            }
            // Keep the green "RX active" flash in sync (clears when a VFO
            // stops playing, or lights up if its squelch just opened).
            this._updateVfoRxActive(i);
        }
    },

    /** Switch the active VFO target for clicks / tune buttons. */
    selectActiveVfo(idx) {
        if (idx < 0 || idx >= this._vfoCount) return;
        this._activeVfoIdx = idx;
        if (typeof LiveAudio !== 'undefined') LiveAudio.setActive(idx);
        this._updateVfoRows();
        this.updateFreqDisplay();
    },

    /** Increase/decrease the number of active VFOs (1..MAX). delta = +1/-1. */
    adjustVfoCount(delta) {
        const next = Math.max(1, Math.min(this._MAX_VFOS, this._vfoCount + delta));
        if (next === this._vfoCount) return;
        this._vfoCount = next;
        if (typeof LiveAudio !== 'undefined') LiveAudio.setVfoCount(next);
        if (delta > 0) {
            // Adding a VFO: auto-select the newly-added VFO (last slot) so the
            // user can tune it immediately without having to click its row
            // first. Otherwise clicks/tune/input would still target the old VFO.
            this._activeVfoIdx = next - 1;
            if (typeof LiveAudio !== 'undefined') LiveAudio.setActive(this._activeVfoIdx);
        } else if (this._activeVfoIdx >= next) {
            // Removing a VFO: keep active index in valid range.
            this._activeVfoIdx = next - 1;
            if (typeof LiveAudio !== 'undefined') LiveAudio.setActive(this._activeVfoIdx);
        }
        this._updateVfoCountBtn();
        this._updateVfoRows();
        this.updateFreqDisplay();
    },

    /** Update the "+VFO/-VFO" buttons' enabled state + label. */
    _updateVfoCountBtn() {
        const lbl = document.getElementById('vfo-count-label');
        if (lbl) lbl.textContent = `VFO ${this._vfoCount}`;
        const add = document.getElementById('btn-vfo-add');
        const sub = document.getElementById('btn-vfo-sub');
        if (add) add.disabled = (this._vfoCount >= this._MAX_VFOS);
        if (sub) sub.disabled = (this._vfoCount <= 1);
    },

    /** Set speaker routing for a VFO from a dropdown change. */
    setVfoSpeaker(idx, mode) {
        if (typeof LiveAudio !== 'undefined') LiveAudio.setSpeaker(idx, mode);
    },

    /** Bind a VFO mute button to BOTH click + long-press (solo/priority).
     *  - LONG-PRESS (≥500ms): toggle SOLO for this VFO (listen to ONLY this
     *    VFO, auto-mute the others). Long-pressing the same soloed VFO again
     *    exits solo and restores each VFO's previous mute state.
     *  - QUICK CLICK: if a solo is active, exit solo (restore prior mute
     *    state). Otherwise toggle this VFO's mute normally. */
    _bindVfoMuteButton(btn, idx) {
        if (!btn) return;
        let holdTimer = null;
        let didHold = false;
        const HOLD_MS = 500;

        const startHold = () => {
            didHold = false;
            holdTimer = setTimeout(() => {
                didHold = true;
                if (typeof LiveAudio === 'undefined') return;
                // Toggle solo for this VFO (soloVfo auto-exits if already solo).
                LiveAudio.soloVfo(idx);
                this._updateVfoRows();
                const id = this._vfoId(idx);
                console.log(`🎧 VFO ${id} ${LiveAudio.isSolo(idx) ? 'SOLO on' : 'SOLO off (restored)'}`);
            }, HOLD_MS);
        };

        const endHold = () => {
            if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
        };

        const onClick = (e) => {
            if (didHold) { didHold = false; return; }  // swallow click after long-press
            if (typeof LiveAudio === 'undefined') return;
            // If a solo is active, a quick click exits solo (restore state).
            if (LiveAudio.anySolo) {
                LiveAudio.exitSolo();
                this._updateVfoRows();
                console.log('🎧 SOLO off via click (restored mute state)');
                return;
            }
            this.toggleVfoMute(idx);
        };

        // Mouse
        btn.addEventListener('mousedown', startHold);
        btn.addEventListener('mouseup', endHold);
        btn.addEventListener('mouseleave', endHold);
        btn.addEventListener('click', onClick);
        // Touch (mobile)
        btn.addEventListener('touchstart', () => startHold(), { passive: true });
        btn.addEventListener('touchend', () => endHold(), { passive: true });
    },

    /** Toggle a VFO's mute. Updates the button icon + the audio graph. */
    toggleVfoMute(idx) {
        if (typeof LiveAudio === 'undefined') return;
        LiveAudio.toggleMute(idx);
        this._updateVfoRows();
    },

    // Server confirmed THIS listener's per-listener squelch (the value the
    // audio WS actually applied). While listening live the SQL slider/input
    // should reflect THIS user's value (not the global status broadcast).
    // We deliberately ignore the global `squelch_level` in
    // syncControlsFromStatus() while live (see the guard there), so this ack
    // is the authoritative source of truth for the slider while listening.
    _onSquelchAck(idx, obj) {
        if (obj.level === undefined || obj.level === null) return;
        const level = Number(obj.level);
        // Don't clobber while the user is mid-edit (re-arming the edit lock).
        if (this._activeEdits.has('squelch')) return;
        const ss = document.getElementById('squelch-slider');
        const si = document.getElementById('squelch-input');
        if (ss) ss.value = level;
        if (si) si.value = level;
    },

    // Server confirmed an auto-squelch toggle for THIS listener. Update the
    // button active state + disable/enable the manual SQL controls.
    _onAutoSquelchAck(obj) {
        this._autoSquelchOn = !!obj.enabled;
        const btn = document.getElementById('sql-auto-btn');
        const slider = document.getElementById('squelch-slider');
        const input = document.getElementById('squelch-input');
        if (btn) {
            btn.textContent = this._autoSquelchOn ? '🎚️ AUTO ●' : '🎚️ AUTO';
            btn.classList.toggle('auto-active', this._autoSquelchOn);
        }
        if (slider) slider.disabled = this._autoSquelchOn;
        if (input) input.disabled = this._autoSquelchOn;
        document.querySelectorAll('.step-mini-btn[data-target="squelch"]').forEach(b => {
            b.disabled = this._autoSquelchOn;
        });
    },

    async _syncRecordingState() {
        try {
            const status = await API.sdr.status();
            // Only sync MANUAL recording state (auto runs independently)
            this.isRecording = !!status.manual_recording;
            const btn = document.getElementById('btn-record');
            if (btn) btn.classList.toggle('recording', this.isRecording);
            // Show auto-recording indicator if active (pass full status)
            this.updateAutoRecordIndicator(status);
        } catch (e) { /* ignore */ }
    },

    updateAutoRecordIndicator(status) {
        // Accept either a full status object or legacy (isAuto, source, data).
        // This keeps backward compatibility with any other callers.
        let isAuto, source, data;
        if (status && typeof status === 'object' && !Array.isArray(status)) {
            data = status;
            isAuto = !!status.auto_recording;
            source = status.recording_source;
        } else {
            isAuto = arguments[0];
            source = arguments[1];
            data = arguments[2] || {};
        }

        // Inline button indicator (next to record button).
        // Show if ANY recording is active (manual / center auto / per-listener
        // auto / job captures) so the icon reflects all activity, not just the
        // global center auto-record.
        const hasManual = !!(data && data.manual_recording);
        const hasListenerRec = !!(data && Array.isArray(data.listener_recordings) &&
            data.listener_recordings.some(l => l.manual_recording || l.auto_recording));
        const hasJobRec = !!(data && Array.isArray(data.active_job_captures) &&
            data.active_job_captures.some(cap =>
                Array.isArray(cap.frequencies) && cap.frequencies.some(f => f.is_recording)
            ));
        const anyActive = isAuto || hasManual || hasListenerRec || hasJobRec;

        let el = document.getElementById('auto-rec-indicator');
        if (anyActive) {
            if (!el) {
                el = document.createElement('span');
                el.id = 'auto-rec-indicator';
                el.style.cssText = 'font-size:0.7rem;color:var(--accent-yellow);margin-left:4px;animation:blink 1s infinite;';
                const btn = document.getElementById('btn-record');
                if (btn && btn.parentNode) btn.parentNode.insertBefore(el, btn.nextSibling);
            }
            el.textContent = '🔄REC';
            el.title = I18n.t('dash.recStatusTitle', source || 'auto');
        } else if (el) {
            el.remove();
        }

        // Recording status list (below clock) — shows ALL active recordings:
        // 1. Manual (center) recording
        // 2. Center auto-record (global)
        // 3. Per-listener manual + auto-record (each user)
        // 4. Background job captures
        const list = document.getElementById('recording-status-list');
        if (!list) return;
        const items = [];

        // Helper: build a colored status row
        const row = (icon, text, color) =>
            `<div class="rec-status-item" style="display:flex;align-items:center;gap:4px;font-size:0.7rem;color:${color || 'var(--accent-yellow)'};margin-top:2px;">`
            + `<span style="animation:blink 1s infinite;">${icon}</span>`
            + `<span>${text}</span>`
            + `</div>`;

        // Helper: compute VFO frequency from center + offset
        const centerFreq = (data && data.frequency) || this.currentFreq || 0;
        const freqFromOffset = (offsetHz) => {
            const f = centerFreq + (offsetHz || 0);
            return (f / 1e6).toFixed(4);
        };

        // 1. Manual recording (center channel, pressed by current user)
        if (hasManual) {
            const freqMhz = freqFromOffset(0);
            const mode = (data && data.mode) || this.currentMode || 'FM';
            items.push(row('🔴', I18n.t('dash.recManual', freqMhz, mode)));
        }

        // 2. Center auto-record (global background)
        if (isAuto) {
            const freqMhz = freqFromOffset(0);
            const mode = (data && data.mode) || this.currentMode || 'FM';
            const bwHz = (data && data.channel_bw) || this._channelBw || 12000;
            const bwKhz = (bwHz / 1000).toFixed(0);
            items.push(row('🔴', I18n.t('dash.recAutoCenter', freqMhz, mode, bwKhz)));
        }

        // 3. Per-listener recordings (manual + auto, each user's own VFO)
        if (data && Array.isArray(data.listener_recordings)) {
            for (const ln of data.listener_recordings) {
                const freqMhz = freqFromOffset(ln.vfo_offset_hz);
                const mode = ln.mode || 'FM';
                const bwKhz = ((ln.channel_bw || 12000) / 1000).toFixed(0);
                const userTag = ln.user_id ? `👤 ${ln.user_id.slice(0, 8)}` : I18n.t('dash.guest');
                if (ln.manual_recording) {
                    items.push(row('🔴', I18n.t('dash.recManualUser', userTag, freqMhz, mode, bwKhz)));
                }
                if (ln.auto_recording) {
                    items.push(row('🎙️', I18n.t('dash.recAutoListen', userTag, freqMhz, mode, bwKhz)));
                }
            }
        }

        // 4. Background job captures
        if (data && Array.isArray(data.active_job_captures)) {
            for (const cap of data.active_job_captures) {
                if (!Array.isArray(cap.frequencies)) continue;
                for (const f of cap.frequencies) {
                    if (!f.is_recording) continue;
                    const freqMhz = (f.frequency_hz / 1e6).toFixed(4);
                    const bwKhz = ((f.bandwidth_hz || 12000) / 1000).toFixed(0);
                    const label = cap.job_label || (cap.job_id ? cap.job_id.slice(0, 8) : 'job');
                    items.push(row('📼', I18n.t('dash.recJob', label, freqMhz, f.demod_mode, bwKhz)));
                }
            }
        }
        list.innerHTML = items.join('');
    },

    // === Volume ===
    initVolumeSlider() {
        const slider = document.getElementById('volume-slider');
        const pct = document.getElementById('volume-pct');
        const icon = document.getElementById('volume-icon');
        const ctrl = document.getElementById('volume-control');
        if (!slider) return;

        const savedVol = parseFloat(localStorage.getItem('audio_volume'));
        const initialPct = (savedVol !== null && !isNaN(savedVol))
            ? Math.round(savedVol * 100)
            : 80;
        slider.value = initialPct;
        if (pct) pct.textContent = initialPct + '%';
        this._updateVolumeIcon(initialPct, icon);
        if (typeof LiveAudio !== 'undefined') {
            LiveAudio.volume = initialPct / 100;
        }

        // Click the speaker icon to toggle mute. We keep a snapshot of the
        // real volume (_preMutePct) so unmuting restores it exactly, and we
        // never persist volume=0 from muting (the saved volume stays intact).
        this._masterMuted = false;
        this._preMutePct = initialPct;
        if (icon) {
            icon.addEventListener('click', () => this._toggleMasterMute());
        }

        slider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value);
            if (pct) pct.textContent = val + '%';
            // Dragging the slider away from 0 clears mute.
            if (val > 0 && this._masterMuted) this._clearMasterMute();
            if (val === 0 && !this._masterMuted) this._applyMasterMute();
            this._updateVolumeIcon(val, icon);
            if (typeof LiveAudio !== 'undefined') {
                LiveAudio.setVolume(val / 100);
            }
        });
    },

    _toggleMasterMute() {
        if (this._masterMuted) this._clearMasterMute();
        else this._applyMasterMute();
    },

    _applyMasterMute() {
        const slider = document.getElementById('volume-slider');
        const icon = document.getElementById('volume-icon');
        const ctrl = document.getElementById('volume-control');
        // Remember the real volume before silencing.
        if (slider && parseInt(slider.value) > 0) {
            this._preMutePct = parseInt(slider.value);
        }
        this._masterMuted = true;
        if (slider) slider.value = 0;
        const pct = document.getElementById('volume-pct');
        if (pct) pct.textContent = '0%';
        if (icon) icon.textContent = '🔇';
        if (ctrl) ctrl.classList.add('muted');
        if (typeof LiveAudio !== 'undefined') LiveAudio.setMuted(true);
        console.log('🔇 Master muted');
    },

    _clearMasterMute() {
        const slider = document.getElementById('volume-slider');
        const icon = document.getElementById('volume-icon');
        const ctrl = document.getElementById('volume-control');
        this._masterMuted = false;
        const restore = Math.max(1, this._preMutePct || 80);
        if (slider) slider.value = restore;
        const pct = document.getElementById('volume-pct');
        if (pct) pct.textContent = restore + '%';
        if (ctrl) ctrl.classList.remove('muted');
        this._updateVolumeIcon(restore, icon);
        if (typeof LiveAudio !== 'undefined') {
            LiveAudio.setMuted(false);
            LiveAudio.setVolume(restore / 100);
        }
        console.log('🔊 Master unmuted → ' + restore + '%');
    },

    _updateVolumeIcon(pct, icon) {
        if (!icon) return;
        if (pct === 0) icon.textContent = '🔇';
        else if (pct < 33) icon.textContent = '🔈';
        else if (pct < 66) icon.textContent = '🔉';
        else icon.textContent = '🔊';
    },

    // === Signal Meter ===
    // The top shared SIG meter was removed; each VFO row has its own compact
    // meter now. When NOT listening live, the global center-channel level
    // (from /ws status) is shown on VFO A's bars so the operator always sees
    // *some* signal indication without having to press ฟังสด first. Once live,
    // per-listener 'sig' frames override this via _renderVfoSig → _updateVfoBars.
    updateSignalMeter(dbm) {
        this.currentSignal = dbm;
        // When listening live, the per-VFO bars are driven by 'sig' frames —
        // don't let the global level clobber them.
        const listening = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
        if (!listening) {
            this._updateVfoBars(0, dbm);
        }
    },

    // Lightweight toast so users know why streaming stopped (instead of the
    // button just silently flipping back to "รับสัญญาณ"). Auto-dismisses.
    _showStreamStoppedToast(msg) {
        const text = msg || I18n.t('dash.streamStopped');
        let el = document.getElementById('stream-toast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'stream-toast';
            el.style.cssText = (
                'position:fixed;bottom:70px;left:50%;transform:translateX(-50%);'
                + 'background:#2b2b2b;color:#ffd166;border:1px solid #ffd166;'
                + 'padding:10px 16px;border-radius:8px;z-index:9999;'
                + 'font-size:0.85rem;box-shadow:0 4px 12px rgba(0,0,0,0.4);'
                + 'max-width:90vw;text-align:center;'
            );
            document.body.appendChild(el);
        }
        el.textContent = '⚠️ ' + text;
        el.style.display = 'block';
        if (this._streamToastTimer) clearTimeout(this._streamToastTimer);
        this._streamToastTimer = setTimeout(() => { el.style.display = 'none'; }, 5000);
    },

    // === WebSocket Listeners ===
    setupWSListeners() {
        WS.on('status', (data) => {
            // Sync all state from backend
            let changed = false;
            
            if (data.connected !== undefined && data.connected !== this.isConnected) {
                this.isConnected = data.connected;
                changed = true;
            }
            if (data.streaming !== undefined && data.streaming !== this.isStreaming) {
                // If the backend stopped streaming shortly after we started it
                // (e.g. RTL-TCP unreachable, dongle unplugged, too many errors),
                // inform the user instead of silently flipping the button back.
                const wasStreaming = this.isStreaming;
                this.isStreaming = data.streaming;
                if (wasStreaming && !data.streaming) {
                    console.warn('📡 Streaming was stopped by the backend.');
                    this._showStreamStoppedToast();
                }
                changed = true;
            }
            if (data.signal_level_db !== undefined) {
                // While any VFO is listening live, the SIG meter is driven by
                // per-listener "sig" frames over the AUDIO WebSocket (each VFO
                // sees its OWN channel power). Ignore the GLOBAL center-channel
                // level here so it can't clobber the per-VFO value(s).
                const listening = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
                if (!listening) {
                    this.updateSignalMeter(data.signal_level_db);
                }
            }
            if (data.frequency) {
                // When the HARDWARE center frequency changes (admin retune, or
                // a background job reclaiming the tuner), every listener's VFO
                // offset is reset to 0 server-side (offsets are relative to
                // the OLD center and would point elsewhere). Mirror that here:
                // reset this user's cursor to the new center so it doesn't
                // dangle outside the new passband.
                const centerChanged = data.frequency !== this.currentFreq;
                this.currentFreq = data.frequency;
                if (centerChanged) {
                    // Center changed → reset EVERY VFO cursor (offsets are
                    // relative to the old center and would point elsewhere).
                    for (let i = 0; i < this._MAX_VFOS; i++) this._setVfoOffset(i, 0);
                    this._vfoUserTouched = false;
                }
                this._waterfallDirty = true;
                this.updateFreqDisplay();
            }
            // VFO cursor is PER-USER (and per-VFO). Once the user moves their
            // own cursor, the global offset broadcast must NEVER overwrite the
            // local display. We only adopt the global value on the very first
            // status sync (before _vfoUserTouched), into VFO A.
            if (data.vfo_offset_hz !== undefined && !this._vfoUserTouched) {
                this._setVfoOffset(0, data.vfo_offset_hz);
                this.updateFreqDisplay();
            }
            if (data.mode) {
                // While listening live, the demod mode is PER-LISTENER (each
                // user picks their own mode via the audio WS). The global
                // center-channel `data.mode` is NOT this user's mode, so
                // adopting it here would snap the active mode button back to
                // the global value every ~1s (the "เด้งกลับ" symptom). Only
                // adopt the global mode when NOT listening live.
                const listeningMode = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
                if (!listeningMode) {
                    this.currentMode = data.mode;
                    document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === data.mode));
                    const rm = document.getElementById('radio-mode');
                    if (rm) rm.textContent = data.mode;
                }
            }
            // Apply per-device waterfall dB range (overrides global when set)
            this._applyDeviceWaterfallRange(data);
            // Sync sliders + SRATE button from backend (single source of truth)
            this.syncControlsFromStatus(data);
            // Sync listener count (live audio)
            if (data.listeners !== undefined) {
                const el = document.getElementById('listener-count');
                if (el) {
                    el.textContent = data.listeners > 0 ? `👥 ${data.listeners}` : '';
                }
            }
            // Sync auto-recording indicator (independent from manual record button).
            // Pass the full status object so the indicator can show ALL active
            // recordings (manual / center auto / per-listener auto / job captures).
            if (data.auto_recording !== undefined || data.recording_source !== undefined ||
                Array.isArray(data.listener_recordings) || Array.isArray(data.active_job_captures)) {
                this.updateAutoRecordIndicator(data);
            }
            if (changed) {
                this.updateUI();
            }
        });

        WS.on('spectrum', (data) => {
            // Decode spectrum data into an Int16Array.
            // Supported shapes (kept for backward compatibility):
            //   - Int16Array (preferred): sent directly from a binary WS frame
            //     (2-byte type tag + int16 LE payload). No base64/JSON overhead.
            //   - string: legacy base64-encoded int16 bytes.
            //   - Array<number>: legacy JSON mode.
            let spectrum;
            if (data instanceof Int16Array) {
                spectrum = data;
            } else if (typeof data === 'string') {
                const binary = atob(data);
                const len = binary.length / 2;
                spectrum = new Int16Array(len);
                for (let i = 0; i < len; i++) {
                    spectrum[i] = (binary.charCodeAt(i*2) | (binary.charCodeAt(i*2+1) << 8)) << 16 >> 16;
                }
            } else if (Array.isArray(data)) {
                spectrum = data;
            } else {
                return;
            }
            this.drawWaterfall(spectrum);
            // Feed the 3D view too (no-op if 3D mode is off).
            this._feedWaterfall3D(spectrum);
        });

        // Per-cursor SIG at 5 FPS (non-live only). Drives VFO A's bar so the
        // operator sees the signal under their cursor update responsively,
        // not just once per second with the full status frame.
        WS.on('sig_cursor', (data) => {
            const listening = (typeof LiveAudio !== 'undefined' && LiveAudio.anyPlaying);
            if (listening) return;  // live mode uses per-listener 'sig' frames
            if (data && typeof data.db === 'number') {
                this._updateVfoBars(0, data.db);
            }
        });

        WS.on('new_recording', (data) => {
            this.addLiveFeedItem(data);
            // If manual recording just finished, clear the button state
            if (data.source === 'manual') {
                this.isRecording = false;
                const btn = document.getElementById('btn-record');
                if (btn) btn.classList.remove('recording');
            }
        });
    },

    // === Waterfall Display (scroll-blit, OpenWebRX style) ===
    // Instead of redrawing all history every frame (O(w*h)), we scroll the
    // existing canvas content down by 1px and draw only the new line at top.
    // This is ~50-100x faster and frees CPU for audio playback.
    _waterfallCanvasW: 0,
    _waterfallCanvasH: 0,
    _waterfallBackBuffer: null,     // offscreen canvas holding waterfall data
    _waterfallBackCtx: null,
    _waterfallLineCanvas: null,     // offscreen canvas at numBins width (for bilinear scaling)
    _waterfallLineCtx: null,
    _waterfallLineImg: null,        // reusable ImageData (numBins × 1)
    _waterfallLineBins: 0,          // track line image width (= numBins)
    _waterfallPendingLines: [],     // queue of new lines to draw
    _waterfallDirty: false,         // need to composite to main canvas
    _waterfallBins: 0,              // native FFT bin count (= back buffer width)
    _waterfallZoomMax: 20,          // max zoom factor

    // === Signal Tag (frequency annotations on strong signals) ===
    _signalAnnotations: [],         // active annotation objects {freq, rightFreq, y, ...}
    _sigTagLastShown: {},           // freqKey → timestamp of last shown annotation
    _sigTagColorIdx: 0,             // color cycle counter for annotations

    // === Zoom & Pan state ===
    // _wfZoom: 1 = full passband visible; >1 = zoomed in.
    // _wfPanOffsetHz shifts the *view* center within the passband
    // (the hardware center frequency is unchanged).
    _wfZoom: 1,
    _wfPanOffsetHz: 0,
    _wfPressActive: false,          // mouse left button held (potential drag)
    _wfPressStartX: 0,
    _wfPressStartPan: 0,
    _wfPressMoved: false,           // did the press turn into a drag?
    _wfSuppressClick: false,        // swallow next click after a drag
    _wfTouch: null,                 // current touch gesture state

    // --- view math helpers (Hz / pixels) ---
    _wfVisibleBw() {
        const sr = this._sampleRate || 2048000;
        return sr / (this._wfZoom || 1);
    },
    _wfViewCenter() {
        return this.currentFreq + (this._wfPanOffsetHz || 0);
    },
    _wfViewLow() {
        return this._wfViewCenter() - this._wfVisibleBw() / 2;
    },
    _wfViewHigh() {
        return this._wfViewCenter() + this._wfVisibleBw() / 2;
    },
    _wfFreqToX(freq, w) {
        return ((freq - this._wfViewLow()) / this._wfVisibleBw()) * w;
    },
    _wfXToFreq(x, w) {
        return this._wfViewLow() + (x / w) * this._wfVisibleBw();
    },
    _wfClampPanValue(off) {
        const sr = this._sampleRate || 2048000;
        const zoom = this._wfZoom || 1;
        const maxOff = Math.max(0, (sr - sr / zoom) / 2);
        return Math.max(-maxOff, Math.min(maxOff, off));
    },
    _wfClampPan() {
        this._wfPanOffsetHz = this._wfClampPanValue(this._wfPanOffsetHz || 0);
    },
    // Apply a new zoom level, keeping anchorFreq under screen x=anchorX.
    _wfSetZoom(zoom, anchorFreq, anchorX, w) {
        zoom = Math.max(1, Math.min(this._waterfallZoomMax, zoom));
        this._wfZoom = zoom;
        if (anchorFreq != null && Number.isFinite(anchorFreq) && w > 0) {
            const visibleBwNew = this._wfVisibleBw();
            const newViewCenter = anchorFreq - (anchorX / w - 0.5) * visibleBwNew;
            this._wfPanOffsetHz = this._wfClampPanValue(newViewCenter - this.currentFreq);
        } else {
            this._wfClampPan();
        }
        this._waterfallDirty = true;
        this._wfUpdateBadge();
        this._updateWaterfallCursor();
    },
    _wfResetZoom() {
        this._wfZoom = 1;
        this._wfPanOffsetHz = 0;
        this._waterfallDirty = true;
        this._wfUpdateBadge();
        this._updateWaterfallCursor();
    },
    _updateWaterfallCursor() {
        const canvas = this.waterfallCanvas;
        if (!canvas) return;
        const zoom = this._wfZoom || 1;
        canvas.style.cursor = zoom > 1.001
            ? (this._wfPressActive ? 'grabbing' : 'grab')
            : 'crosshair';
    },
    _wfEnsureBadge() {
        let el = document.getElementById('wf-zoom-badge');
        if (!el) {
            el = document.createElement('div');
            el.id = 'wf-zoom-badge';
            el.className = 'wf-zoom-badge';
            const cont = this.waterfallCanvas ? this.waterfallCanvas.parentElement : null;
            if (cont) cont.appendChild(el);
        }
        return el;
    },
    _wfUpdateBadge() {
        const el = this._wfEnsureBadge();
        if (!el) return;
        if ((this._wfZoom || 1) > 1.001) {
            el.textContent = `🔍 ${this._wfZoom.toFixed(1)}x`;
            el.style.display = '';
        } else {
            el.style.display = 'none';
        }
    },
    // Ensure offscreen back buffer is at native FFT-bin resolution
    // (width = numBins) and matches the current canvas height.
    // Content is preserved across height changes; cleared on bin-count change.
    _ensureBackBuffer(numBins) {
        const h = this._waterfallCanvasH;
        if (!Number.isFinite(numBins) || numBins <= 0 || h <= 0) return;
        const bb = this._waterfallBackBuffer;
        if (bb && bb.width === numBins && bb.height === h) return;
        // Preserve old content only when bin width is unchanged (height change).
        let oldCanvas = null;
        if (bb && bb.width === numBins && bb.height > 0) {
            oldCanvas = document.createElement('canvas');
            oldCanvas.width = bb.width;
            oldCanvas.height = bb.height;
            oldCanvas.getContext('2d').drawImage(bb, 0, 0);
        }
        if (!this._waterfallBackBuffer) {
            this._waterfallBackBuffer = document.createElement('canvas');
            this._waterfallBackCtx = this._waterfallBackBuffer.getContext('2d');
        }
        this._waterfallBackBuffer.width = numBins;
        this._waterfallBackBuffer.height = h;
        const bbCtx = this._waterfallBackCtx;
        bbCtx.fillStyle = '#0a0a1a';
        bbCtx.fillRect(0, 0, numBins, h);
        if (oldCanvas) bbCtx.drawImage(oldCanvas, 0, 0);
        this._waterfallBins = numBins;
        this._waterfallLineBins = 0;  // force line canvas rebuild
        this._waterfallDirty = true;
    },

    initWaterfall() {
        const canvas = document.getElementById('waterfall-canvas');
        if (!canvas) return;
        this.waterfallCanvas = canvas;
        this.waterfallCtx = canvas.getContext('2d');

        // Offscreen back buffer holds only waterfall pixels (no grid).
        this._waterfallBackBuffer = document.createElement('canvas');
        this._waterfallBackCtx = this._waterfallBackBuffer.getContext('2d');

        const resize = () => {
            const parent = canvas.parentElement;
            if (!parent) return;
            const w = parent.clientWidth;
            const h = parent.clientHeight;
            if (w > 0 && h > 0 && (w !== this._waterfallCanvasW || h !== this._waterfallCanvasH)) {
                this._waterfallCanvasW = w;
                this._waterfallCanvasH = h;
                canvas.width = w;
                canvas.height = h;
                // Fill background so canvas isn't blank before the first frame.
                this.waterfallCtx.fillStyle = '#0a0a1a';
                this.waterfallCtx.fillRect(0, 0, w, h);
                // Back buffer is native-bin resolution — re-ensure on height change.
                if (this._waterfallBins > 0) this._ensureBackBuffer(this._waterfallBins);
                this._waterfallDirty = true;
            }
        };

        if (window.ResizeObserver && canvas.parentElement) {
            new ResizeObserver(resize).observe(canvas.parentElement);
        }
        window.addEventListener('resize', resize);
        requestAnimationFrame(resize);

        // === Zoom & Pan interaction ===
        this._hoverFreqEl = document.getElementById('waterfall-hover-freq');
        this._hoverFreqTimer = null;
        this._wfPressActive = false;
        this._wfSuppressClick = false;
        this._wfTouch = null;
        this._updateWaterfallCursor();
        this._wfUpdateBadge();

        // --- Mouse wheel: zoom in/out anchored at the cursor ---
        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const w = rect.width;
            const anchorFreq = this._wfXToFreq(x, w);
            const factor = e.deltaY < 0 ? 1.25 : 1 / 1.25;
            this._wfSetZoom((this._wfZoom || 1) * factor, anchorFreq, x, w);
        }, { passive: false });

        // --- Double click: reset zoom to 1x ---
        canvas.addEventListener('dblclick', () => this._wfResetZoom());

        // --- Mouse drag to pan (when zoomed); a plain click sets the VFO ---
        canvas.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            this._wfPressActive = true;
            this._wfPressStartX = e.clientX;
            this._wfPressStartPan = this._wfPanOffsetHz || 0;
            this._wfPressMoved = false;
            this._wfSuppressClick = false;
            this._updateWaterfallCursor();
        });

        canvas.addEventListener('mousemove', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const w = rect.width;
            // Drag-pan while the left button is held.
            if (this._wfPressActive && (e.buttons & 1)) {
                const dx = e.clientX - this._wfPressStartX;
                if (!this._wfPressMoved && Math.abs(dx) > 4) this._wfPressMoved = true;
                if (this._wfPressMoved) {
                    this._wfSuppressClick = true;
                    const visibleBw = this._wfVisibleBw();
                    const dHz = -(dx / w) * visibleBw;
                    this._wfPanOffsetHz = this._wfClampPanValue(this._wfPressStartPan + dHz);
                    this._waterfallDirty = true;
                    this._wfUpdateBadge();
                    if (this._hoverFreqEl) this._hoverFreqEl.classList.remove('visible');
                    return;
                }
            }
            // Hover frequency tooltip (shown after 0.3s dwell)
            if (!this._hoverFreqEl) return;
            const clampedX = Math.max(0, Math.min(x, rect.width));
            this._hoverFreqEl.style.left = `${clampedX}px`;
            this._hoverFreqEl.style.top = `${e.clientY - rect.top}px`;
            this._hoverFreqEl.classList.remove('visible');
            if (this._hoverFreqTimer) clearTimeout(this._hoverFreqTimer);
            this._hoverFreqTimer = setTimeout(() => {
                if (!this._hoverFreqEl) return;
                const freq = this._wfXToFreq(clampedX, rect.width);
                this._hoverFreqEl.textContent = this._formatFreq(freq);
                this._hoverFreqEl.classList.add('visible');
            }, 300);
        });

        const endPress = () => {
            this._wfPressActive = false;
            this._updateWaterfallCursor();
        };
        canvas.addEventListener('mouseup', () => endPress());
        canvas.addEventListener('mouseleave', () => {
            endPress();
            if (this._hoverFreqTimer) { clearTimeout(this._hoverFreqTimer); this._hoverFreqTimer = null; }
            if (this._hoverFreqEl) this._hoverFreqEl.classList.remove('visible');
        });

        // Click → set VFO (swallowed if it followed a drag).
        canvas.addEventListener('click', (e) => {
            if (this._wfSuppressClick) { this._wfSuppressClick = false; return; }
            this._onWaterfallClick(e);
        });

        // --- Touch gestures ---
        // 1 finger: drag = pan, tap = set VFO
        // 2 fingers: pinch = zoom (anchored at midpoint)
        canvas.addEventListener('touchstart', (e) => {
            if (e.touches.length === 1) {
                const t = e.touches[0];
                this._wfTouch = {
                    mode: 'pan',
                    startX: t.clientX,
                    startPan: this._wfPanOffsetHz || 0,
                    moved: false,
                };
            } else if (e.touches.length === 2) {
                const a = e.touches[0], b = e.touches[1];
                const rect = canvas.getBoundingClientRect();
                const midX = ((a.clientX + b.clientX) / 2) - rect.left;
                this._wfTouch = {
                    mode: 'pinch',
                    startDist: Math.max(1, Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)),
                    startZoom: this._wfZoom || 1,
                    anchorX: midX,
                    anchorFreq: this._wfXToFreq(midX, rect.width),
                };
            }
        }, { passive: true });

        canvas.addEventListener('touchmove', (e) => {
            if (!this._wfTouch) return;
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const w = rect.width;
            if (this._wfTouch.mode === 'pan' && e.touches.length === 1) {
                const t = e.touches[0];
                const dx = t.clientX - this._wfTouch.startX;
                if (!this._wfTouch.moved && Math.abs(dx) > 8) this._wfTouch.moved = true;
                if (this._wfTouch.moved) {
                    const visibleBw = this._wfVisibleBw();
                    const dHz = -(dx / w) * visibleBw;
                    this._wfPanOffsetHz = this._wfClampPanValue(this._wfTouch.startPan + dHz);
                    this._waterfallDirty = true;
                    this._wfUpdateBadge();
                }
            } else if (this._wfTouch.mode === 'pinch' && e.touches.length === 2) {
                const a = e.touches[0], b = e.touches[1];
                const dist = Math.max(1, Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY));
                const scale = dist / this._wfTouch.startDist;
                this._wfSetZoom(this._wfTouch.startZoom * scale, this._wfTouch.anchorFreq, this._wfTouch.anchorX, w);
            }
        }, { passive: false });

        canvas.addEventListener('touchend', (e) => {
            const t = this._wfTouch;
            // Tap (single finger, no move) → set VFO at the touch point.
            if (t && t.mode === 'pan' && !t.moved && e.changedTouches.length === 1) {
                const ct = e.changedTouches[0];
                this._onWaterfallClick({ clientX: ct.clientX, clientY: ct.clientY });
            }
            if (e.touches.length === 0) {
                this._wfTouch = null;
            } else if (e.touches.length === 1) {
                // Transition from pinch back to one-finger pan seamlessly.
                const f = e.touches[0];
                this._wfTouch = {
                    mode: 'pan',
                    startX: f.clientX,
                    startPan: this._wfPanOffsetHz || 0,
                    moved: true,
                };
            }
        }, { passive: true });
        canvas.addEventListener('touchcancel', () => { this._wfTouch = null; }, { passive: true });

        // Start render loop
        this._waterfallRenderLoop();
    },

    _waterfallRenderLoop() {
        this._renderWaterfall();
        requestAnimationFrame(() => this._waterfallRenderLoop());
    },

    _dbMin: -110,
    _dbMax: -55,
    _colorLUT: null,  // Color Lookup Table (256 entries) for fast rendering
    _colorLUTDbMin: null,
    _colorLUTDbMax: null,

    _buildColorLUT() {
        // Build a 1024-entry RGB lookup table using HSL color wheel.
        // This produces a *continuous* gradient (no hard band transitions)
        // similar to OpenWebRX's default colormap.
        // Color stops (HSL hue 240→0, i.e. blue→red):
        //   0.00 → dark navy    (hue 250, sat 80%, light 8%)
        //   0.20 → blue         (hue 220, sat 85%, light 35%)
        //   0.40 → cyan         (hue 185, sat 90%, light 50%)
        //   0.60 → green        (hue 120, sat 85%, light 45%)
        //   0.80 → yellow       (hue 50,  sat 95%, light 55%)
        //   1.00 → red/white    (hue 0,   sat 95%, light 65%)
        const N = 1024;
        const lut = new Uint8ClampedArray(N * 3);
        const stops = [
            [0.00, 250, 80, 8],
            [0.20, 220, 85, 35],
            [0.40, 185, 90, 50],
            [0.60, 120, 85, 45],
            [0.80, 50,  95, 55],
            [1.00, 0,   95, 65],
        ];
        for (let i = 0; i < N; i++) {
            const norm = i / (N - 1);
            // Find segment
            let seg = 0;
            while (seg < stops.length - 2 && norm > stops[seg + 1][0]) seg++;
            const s0 = stops[seg], s1 = stops[seg + 1];
            const t = (norm - s0[0]) / (s1[0] - s0[0]);
            // Interpolate HSL
            let h = s0[1] + t * (s1[1] - s0[1]);
            let s = (s0[2] + t * (s1[2] - s0[2])) / 100;
            let l = (s0[3] + t * (s1[3] - s0[3])) / 100;
            const [r, g, b] = this._hslToRgb(h, s, l);
            lut[i * 3]     = r;
            lut[i * 3 + 1] = g;
            lut[i * 3 + 2] = b;
        }
        this._colorLUT = lut;
        this._colorLUTN = N;
        this._colorLUTDbMin = this._dbMin;
        this._colorLUTDbMax = this._dbMax;
    },

    _hslToRgb(h, s, l) {
        // Standard HSL→RGB. h in degrees [0,360], s/l in [0,1].
        h = ((h % 360) + 360) % 360;
        const c = (1 - Math.abs(2 * l - 1)) * s;
        const hp = h / 60;
        const x = c * (1 - Math.abs((hp % 2) - 1));
        let r1 = 0, g1 = 0, b1 = 0;
        if (hp < 1)      { r1 = c; g1 = x; }
        else if (hp < 2) { r1 = x; g1 = c; }
        else if (hp < 3) { g1 = c; b1 = x; }
        else if (hp < 4) { g1 = x; b1 = c; }
        else if (hp < 5) { r1 = x; b1 = c; }
        else             { r1 = c; b1 = x; }
        const m = l - c / 2;
        return [
            Math.round((r1 + m) * 255),
            Math.round((g1 + m) * 255),
            Math.round((b1 + m) * 255),
        ];
    },

    _renderWaterfall() {
        // Read live settings from localStorage (only when NOT in auto mode
        // AND no per-device override is active — _applyDeviceWaterfallRange
        // sets _dbMin/_dbMax directly from the active device config).
        if (this._autoMode === 'off' && !this._deviceWaterfallActive) {
            this._dbMin = this.waterfallDbMin;
            this._dbMax = this.waterfallDbMax;
        }
        const canvas = this.waterfallCanvas;
        const ctx = this.waterfallCtx;
        if (!canvas || !ctx) return;

        const w = this._waterfallCanvasW;
        const h = this._waterfallCanvasH;
        if (w <= 0 || h <= 0) return;

        const bb = this._waterfallBackBuffer;
        const bbCtx = this._waterfallBackCtx;
        if (!bb || !bbCtx) return;

        // Rebuild color LUT if dB range changed
        if (!this._colorLUT || this._colorLUTDbMin !== this._dbMin || this._colorLUTDbMax !== this._dbMax) {
            this._buildColorLUT();
        }
        const lut = this._colorLUT;
        const lutN = this._colorLUTN || 1024;
        const lutNMinus1 = lutN - 1;
        const dbRange = this._dbMax - this._dbMin;
        const dbMin = this._dbMin;
        const dbScale = lutNMinus1 / dbRange;

        // Drain pending lines into the back buffer (scroll-blit).
        // Back buffer is kept at NATIVE FFT-bin resolution (width = numBins)
        // so that zooming in reveals real detail instead of upscaled blur.
        const pending = this._waterfallPendingLines;
        if (pending.length > 0) {
            const firstLine = pending[pending.length - 1];
            const numBins = firstLine.length;
            if (numBins <= 0) {
                pending.length = 0;
            } else {
                this._ensureBackBuffer(numBins);
                const bbW = numBins;  // back buffer width = native FFT bins
                const numNew = Math.min(pending.length, h);
                // Scroll existing content DOWN by numNew pixels.
                if (numNew < h) {
                    bbCtx.drawImage(bb, 0, 0, bbW, h - numNew, 0, numNew, bbW, h - numNew);
                }
                // Clear the top numNew rows
                bbCtx.fillStyle = '#0a0a1a';
                bbCtx.fillRect(0, 0, bbW, numNew);

                // Setup line canvas at numBins width (1:1 with the back buffer)
                if (this._waterfallLineBins !== numBins || !this._waterfallLineCanvas) {
                    this._waterfallLineCanvas = document.createElement('canvas');
                    this._waterfallLineCanvas.width = numBins;
                    this._waterfallLineCanvas.height = 1;
                    this._waterfallLineCtx = this._waterfallLineCanvas.getContext('2d');
                    this._waterfallLineImg = this._waterfallLineCtx.createImageData(numBins, 1);
                    this._waterfallLineBins = numBins;
                }
                const lineCanvas = this._waterfallLineCanvas;
                const lineCtx = this._waterfallLineCtx;
                const imgData = this._waterfallLineImg;
                const pixels = imgData.data;

                bbCtx.imageSmoothingEnabled = true;
                bbCtx.imageSmoothingQuality = 'high';

                for (let i = 0; i < numNew; i++) {
                    const line = pending[pending.length - numNew + i];
                    const y = numNew - 1 - i;

                    // Render spectrum into ImageData at native FFT-bin resolution
                    for (let x = 0; x < numBins; x++) {
                        const val = line[x];
                        let lutIdx = ((val - dbMin) * dbScale) | 0;
                        if (lutIdx < 0) lutIdx = 0;
                        else if (lutIdx > lutNMinus1) lutIdx = lutNMinus1;
                        const lutOff = lutIdx * 3;
                        const off = x * 4;
                        pixels[off] = lut[lutOff];
                        pixels[off + 1] = lut[lutOff + 1];
                        pixels[off + 2] = lut[lutOff + 2];
                        pixels[off + 3] = 255;
                    }
                    lineCtx.putImageData(imgData, 0, 0);

                    // 1:1 blit (native resolution) with slight alpha blend for
                    // smoother vertical transitions.
                    bbCtx.globalAlpha = 0.85;
                    bbCtx.drawImage(lineCanvas, 0, 0, numBins, 1, 0, y, numBins, 1);
                    bbCtx.globalAlpha = 1.0;
                }
                pending.length = 0;
                this._waterfallDirty = true;
            }
        }

        // Composite the visible (zoomed) slice of the back buffer to the main
        // canvas, then draw grid + overlays on top.
        if (this._waterfallDirty) {
            const bbW = this._waterfallBins || (this._waterfallBackBuffer ? this._waterfallBackBuffer.width : w);
            const sr = this._sampleRate || 2048000;
            const lowHzFull = this.currentFreq - sr / 2;  // back buffer left edge freq
            const viewLowHz = this._wfViewLow();
            const viewBwHz = this._wfVisibleBw();
            // Source rectangle (back-buffer pixel space) for the visible window.
            let srcX = ((viewLowHz - lowHzFull) / sr) * bbW;
            let srcW = (viewBwHz / sr) * bbW;
            // Clamp to back-buffer bounds to avoid drawImage artifacts.
            srcX = Math.max(0, Math.min(bbW - 1, srcX));
            srcW = Math.max(1, Math.min(bbW - srcX, srcW));
            ctx.imageSmoothingEnabled = true;
            ctx.imageSmoothingQuality = 'high';
            ctx.drawImage(this._waterfallBackBuffer, srcX, 0, srcW, h, 0, 0, w, h);
            this._drawFreqGrid(ctx, w, h);
            // Draw one cursor per active VFO (up to _vfoCount). The active VFO
            // is drawn LAST so it sits on top of the others.
            const cnt = this._vfoCount;
            // Channel-BW overlay for all active VFOs.
            for (let i = 0; i < cnt; i++) {
                this._drawChannelBwOverlay(ctx, w, h, this._vfos[i].offsetHz);
            }
            // Cursor draw order: non-active first, active last.
            const order = [];
            for (let i = 0; i < cnt; i++) if (i !== this._activeVfoIdx) order.push(i);
            order.push(this._activeVfoIdx);
            for (const i of order) {
                if (i >= cnt) continue;
                this._drawVFOCursor(ctx, w, h, this._vfos[i].offsetHz,
                                    this._vfoColorTpl(i), i === this._activeVfoIdx,
                                    this._vfos[i].id);
            }
            this._drawSignalAnnotations(ctx, w, h);
            this._waterfallDirty = false;
        }
    },

    /**
     * Draw channel bandwidth overlay — corner brackets at top & bottom of
     * waterfall + faint dashed vertical edge lines.
     * No label pill (would overlap frequency tick labels at top).
     *
     *   ┊┌               ┐┊
     *   ┊      ↓ 93.0    ┊
     *   ┊   (waterfall)  ┊
     *   ┊└               ┘┊
     */
    _drawChannelBwOverlay(ctx, w, h, offsetHz) {
        const bw = this._channelBw || 12000;
        const viewBw = this._wfVisibleBw();
        const bwPx = Math.min(w, (bw / viewBw) * w);
        if (bwPx < 4) return;  // too small to draw

        // Draw at the given VFO position (center + offset) within the current
        // zoom view. offsetHz is per-VFO (A or B).
        const cx = this._wfFreqToX(this.currentFreq + (offsetHz || 0), w);

        // Bandwidth rectangle depends on the demodulation mode:
        //  - FM / AM : symmetrical around the carrier (full double-sideband)
        //  - USB     : upper sideband only → rectangle extends to the RIGHT of cx
        //  - LSB     : lower sideband only → rectangle extends to the LEFT of cx
        // This matches the backend SSB phasing demodulator, which shifts up
        // (USB) or down (LSB) and keeps only one sideband.
        let left, right;
        const mode = (this.currentMode || '').toUpperCase();
        if (mode === 'USB') {
            left = cx;
            right = cx + bwPx;
        } else if (mode === 'LSB') {
            left = cx - bwPx;
            right = cx;
        } else {
            left = cx - bwPx / 2;
            right = cx + bwPx / 2;
        }

        ctx.save();

        // === Faint dashed vertical edge lines (full height) ===
        ctx.strokeStyle = 'rgba(234, 179, 8, 0.35)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(Math.round(left) + 0.5, 0);
        ctx.lineTo(Math.round(left) + 0.5, h);
        // Center line (marks center frequency)
        ctx.moveTo(Math.round(cx) + 0.5, 0);
        ctx.lineTo(Math.round(cx) + 0.5, h);
        ctx.moveTo(Math.round(right) + 0.5, 0);
        ctx.lineTo(Math.round(right) + 0.5, h);
        ctx.stroke();
        ctx.setLineDash([]);

        // === Corner brackets (top & bottom) ===
        const armH = 10;       // vertical arm length
        const armW = 6;        // horizontal arm length (corner tick)
        ctx.strokeStyle = 'rgba(234, 179, 8, 0.85)';
        ctx.lineWidth = 2;

        // Top-left corner
        ctx.beginPath();
        ctx.moveTo(left, armH);
        ctx.lineTo(left, 0);
        ctx.lineTo(left + armW, 0);
        ctx.stroke();

        // Top-right corner
        ctx.beginPath();
        ctx.moveTo(right, armH);
        ctx.lineTo(right, 0);
        ctx.lineTo(right - armW, 0);
        ctx.stroke();

        // Bottom-left corner
        ctx.beginPath();
        ctx.moveTo(left, h - armH);
        ctx.lineTo(left, h);
        ctx.lineTo(left + armW, h);
        ctx.stroke();

        // Bottom-right corner
        ctx.beginPath();
        ctx.moveTo(right, h - armH);
        ctx.lineTo(right, h);
        ctx.lineTo(right - armW, h);
        ctx.stroke();

        ctx.restore();
    },

    /**
     * Draw VFO cursor — dashed red vertical line + ▼ wings marker at the
     * listening frequency. Drawn on every dirty frame so it tracks clicks.
     *
     * If the listening frequency is outside the current passband
     * (|vfoOffsetHz| > 0.95 × sampleRate/2) — which can happen transiently
     * when a background job retunes the shared tuner — the cursor is
     * CLAMPED to the near edge of the canvas and drawn dimmed with an
     * "off-screen" label, so it is never invisible.
     */
    _drawVFOCursor(ctx, w, h, offsetHz, colorTpl, isActive, label) {
        // colorTpl is like 'rgba(255, 80, 80, %a)' with %a as the alpha slot.
        const vfoHz = this.currentFreq + (offsetHz || 0);
        const offscreen = !this._isOffsetInPassband(offsetHz);
        // Raw (un-clamped) position; may be < 0 or > w when off-screen.
        const rawCx = this._wfFreqToX(vfoHz, w);
        // Clamp into [0, w] so the marker always stays visible.
        const cx = Math.max(0, Math.min(w, rawCx));

        ctx.save();

        // Active VFO is drawn slightly thicker so it reads as "selected".
        const lw = isActive ? 2.0 : 1.5;
        if (offscreen) {
            ctx.strokeStyle = colorTpl.replace('%a', '0.3');
            ctx.lineWidth = lw;
            ctx.setLineDash([3, 4]);
        } else {
            ctx.strokeStyle = colorTpl.replace('%a', isActive ? '0.85' : '0.6');
            ctx.lineWidth = lw;
            ctx.setLineDash([10, 5]);
        }
        ctx.beginPath();
        ctx.moveTo(Math.round(cx) + 0.5, 0);
        ctx.lineTo(Math.round(cx) + 0.5, h);
        ctx.stroke();

        // ▼ wings marker at top
        ctx.setLineDash([]);
        ctx.strokeStyle = colorTpl.replace('%a', offscreen ? '0.5' : '0.95');
        ctx.lineWidth = 2;
        const armW = 8;
        ctx.beginPath();
        ctx.moveTo(cx - armW, 0);
        ctx.lineTo(cx, 7);
        ctx.lineTo(cx + armW, 0);
        ctx.stroke();

        // VFO label badge (A / B) just under the wings, so the operator can
        // tell the two cursors apart in dual mode.
        if (label) {
            ctx.fillStyle = colorTpl.replace('%a', '0.95');
            ctx.font = 'bold 11px monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, cx, 10);
            ctx.textAlign = 'start';  // reset
        }

        // "◀ off-screen" / "off-screen ▶" label when the VFO is outside band.
        if (offscreen) {
            ctx.setLineDash([]);
            ctx.fillStyle = colorTpl.replace('%a', '0.85');
            ctx.font = '11px monospace';
            ctx.textBaseline = 'top';
            const text = rawCx < 0 ? `◀ ${label||'VFO'} off-screen` : `${label||'VFO'} off-screen ▶`;
            const tx = rawCx < 0 ? 4 : Math.max(4, w - 130);
            ctx.fillText(text, tx, 12);
        }

        ctx.restore();
    },

    /**
     * Whether a given VFO offset places the listening frequency inside the
     * usable passband (|offset| ≤ 0.95 × sampleRate/2). Used to flag an
     * off-screen cursor and to warn in the frequency display.
     */
    _isOffsetInPassband(offsetHz) {
        const sr = this._sampleRate || 0;
        if (sr <= 0) return true;
        const maxOff = sr * 0.5 * 0.95;
        return Math.abs(offsetHz || 0) <= maxOff;
    },
    /** Convenience: passband check for the ACTIVE VFO. */
    _isVfoInPassband() { return this._isOffsetInPassband(this._vfoOffsetHz); },

    /**
     * Draw signal frequency annotations — vertical "sticks" with the frequency
     * number, positioned to the right of strong signals (above SQL threshold).
     * Each annotation scrolls down with its signal (y tracks waterfall scroll).
     *
     * Display rules:
     *   - A frequency can only be annotated again 10s after its last annotation
     *   - Nearby simultaneous signals are staggered by 5s
     */
    _drawSignalAnnotations(ctx, w, h) {
        if (!this.signalTagEnabled) return;

        const colors = ['#22d3ee', '#c084fc', '#f472b6', '#fb923c', '#4ade80', '#facc15'];

        ctx.save();
        ctx.font = "bold 10px 'JetBrains Mono', monospace";

        for (const ann of this._signalAnnotations) {
            if (!ann.visible) continue;
            if (ann.y < -30 || ann.y > h + 30) continue;

            const color = colors[ann.colorIdx % colors.length];
            const signalX = this._wfFreqToX(ann.freq, w);
            const rightX = this._wfFreqToX(ann.rightFreq, w);
            // Skip if signal peak is outside the visible window
            if (signalX < -20 || signalX > w + 20) continue;

            // Position stick to the RIGHT of the signal with padding (flip left if no room)
            const padding = 12;
            let stickX, connectorStart;
            if (rightX + padding <= w - 4) {
                stickX = rightX + padding;
                connectorStart = rightX;
            } else if (signalX - padding >= 4) {
                stickX = signalX - padding;
                connectorStart = signalX;
            } else {
                stickX = Math.max(4, Math.min(w - 4, rightX));
                connectorStart = signalX;
            }
            const y = ann.y;

            // Horizontal connector from signal edge to stick
            ctx.strokeStyle = color;
            ctx.lineWidth = 1;
            ctx.globalAlpha = 0.7;
            ctx.beginPath();
            ctx.moveTo(connectorStart, y);
            ctx.lineTo(stickX, y);
            ctx.stroke();

            // Small dot at signal peak
            ctx.globalAlpha = 1.0;
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(signalX, y, 2.5, 0, Math.PI * 2);
            ctx.fill();

            // Vertical stick
            ctx.globalAlpha = 0.9;
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(stickX, y - 18);
            ctx.lineTo(stickX, y + 18);
            ctx.stroke();

            // Frequency text rotated (reading bottom-to-top)
            ctx.save();
            ctx.translate(stickX, y);
            ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.globalAlpha = 1.0;
            ctx.fillStyle = color;
            const label = (ann.freq / 1e6).toFixed(4);
            ctx.shadowColor = 'rgba(0,0,0,0.95)';
            ctx.shadowBlur = 4;
            ctx.fillText(label, 0, 0);
            ctx.restore();
        }

        ctx.restore();
    },

    _drawFreqGrid(ctx, w, h) {
        const centerHz = this.currentFreq;
        // Grid covers the *visible* (zoomed) window, not the full passband.
        const lowHz = this._wfViewLow();
        const highHz = this._wfViewHigh();
        const bwHz = highHz - lowHz;

        // Choose "nice" tick interval based on the visible bandwidth.
        // Target ~6-10 grid lines across the width.
        const targetCount = 8;
        const rawStep = bwHz / targetCount;
        const steps = [1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000, 2500000, 5000000, 10000000];
        let stepHz = steps[steps.length - 1];
        for (const s of steps) {
            if (s >= rawStep) { stepHz = s; break; }
        }

        // Find first tick >= lowHz
        let tickHz = Math.ceil(lowHz / stepHz) * stepHz;

        ctx.save();
        ctx.font = "10px 'JetBrains Mono', monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";

        while (tickHz <= highHz) {
            const x = ((tickHz - lowHz) / bwHz) * w;
            const isCenter = Math.abs(tickHz - centerHz) < stepHz * 0.01;

            // Vertical grid line (skip center — marked by BW overlay + label pill)
            if (!isCenter) {
                ctx.strokeStyle = "rgba(255, 255, 255, 0.18)";
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(Math.round(x) + 0.5, 0);
                ctx.lineTo(Math.round(x) + 0.5, h);
                ctx.stroke();
            }

            // Frequency label at top
            const label = this._formatFreqTick(tickHz);
            const labelY = 4;
            const labelW = ctx.measureText(label).width + 8;

            // Label background pill
            ctx.fillStyle = isCenter ? "rgba(234, 179, 8, 0.85)" : "rgba(0, 0, 0, 0.65)";
            ctx.fillRect(Math.round(x - labelW / 2), labelY, labelW, 14);

            // Label border for center
            if (isCenter) {
                ctx.strokeStyle = "rgba(0,0,0,0.4)";
                ctx.lineWidth = 1;
                ctx.strokeRect(Math.round(x - labelW / 2) + 0.5, labelY + 0.5, labelW - 1, 13);
            }

            // Label text
            ctx.fillStyle = isCenter ? "#1a2235" : "#e2e8f0";
            ctx.fillText(label, x, labelY + 2);

            tickHz += stepHz;
        }
        ctx.restore();
    },

    _formatFreqTick(hz) {
        // Format based on magnitude
        if (hz >= 1e6) return (hz / 1e6).toFixed(hz % 1e6 === 0 ? 0 : 3) + "M";
        if (hz >= 1e3) return (hz / 1e3).toFixed(0) + "k";
        return String(hz);
    },

    // Format frequency for display in MHz with 3-4 decimal places.
    // Used by the hover tooltip on the waterfall.
    _formatFreq(hz) {
        if (!hz || isNaN(hz)) return '---';
        const mhz = hz / 1e6;
        if (mhz >= 100) return mhz.toFixed(3) + ' MHz';
        return mhz.toFixed(4) + ' MHz';
    },

    // Waterfall speed: how many lines to push per spectrum frame
    get waterfallSpeed() {
        return parseInt(localStorage.getItem('waterfall_speed') || '1');
    },
    set waterfallSpeed(val) {
        localStorage.setItem('waterfall_speed', val);
    },

    // Signal Tag enabled (persisted in localStorage, default ON)
    get signalTagEnabled() {
        return localStorage.getItem('signal_tag_enabled') !== 'false';
    },
    set signalTagEnabled(val) {
        localStorage.setItem('signal_tag_enabled', String(val));
    },

    // Waterfall dB range (global, from localStorage / Display tab).
    // NOTE: Per-device values from the active device's config take priority
    // when "ฟังสด" is active — see _applyDeviceWaterfallRange().
    get waterfallDbMin() {
        return parseInt(localStorage.getItem('waterfall_db_min') || '-110');
    },
    set waterfallDbMin(val) {
        localStorage.setItem('waterfall_db_min', val);
        this._dbMin = val;
    },
    get waterfallDbMax() {
        return parseInt(localStorage.getItem('waterfall_db_max') || '-55');
    },
    set waterfallDbMax(val) {
        localStorage.setItem('waterfall_db_max', val);
        this._dbMax = val;
    },

    // Apply per-device waterfall dB range from SDR status.
    // Called whenever status is updated. Priority:
    //   1. Active device config (waterfall_db_min/max from backend)
    //   2. localStorage (Display tab global setting)
    //   3. Hardcoded default (-110 / -55)
    //
    // GUARDS (to avoid clobbering user/AUTO adjustments every ~1s):
    //   A. If AUTO mode is active ('once' or 'cont'), do NOT overwrite — the
    //      AUTO logic in _renderWaterfall() owns _dbMin/_dbMax until it's done.
    //   B. If the active device hasn't changed since the last call, do NOT
    //      overwrite — device config is static, so re-applying it every status
    //      tick would just snap back any manual/AUTO adjustment. We only apply
    //      when the device actually changes (or on the very first call).
    _applyDeviceWaterfallRange(status) {
        // Guard A: AUTO mode owns the dB range while active.
        if (this._autoMode === 'once' || this._autoMode === 'cont') {
            return;
        }
        // Guard B: only (re)apply when the active device changes.
        const devId = status && status.device_id != null ? String(status.device_id) : null;
        if (devId !== null && devId === this._lastAppliedDeviceId) {
            // Same device as before — don't clobber manual/AUTO values.
            // Still keep the per-device-override flag in sync for rendering.
            const devMin0 = status && status.waterfall_db_min != null ? Number(status.waterfall_db_min) : null;
            const devMax0 = status && status.waterfall_db_max != null ? Number(status.waterfall_db_max) : null;
            this._deviceWaterfallActive = (devMin0 != null && devMax0 != null);
            return;
        }
        if (devId !== null) this._lastAppliedDeviceId = devId;

        const devMin = status && status.waterfall_db_min != null ? Number(status.waterfall_db_min) : null;
        const devMax = status && status.waterfall_db_max != null ? Number(status.waterfall_db_max) : null;
        // Mark whether a per-device override is active. When active,
        // _renderWaterfall() will NOT overwrite _dbMin/_dbMax from localStorage.
        this._deviceWaterfallActive = (devMin != null && devMax != null);
        const newMin = Number.isFinite(devMin) ? devMin : this.waterfallDbMin;
        const newMax = Number.isFinite(devMax) ? devMax : this.waterfallDbMax;
        if (newMin !== this._dbMin || newMax !== this._dbMax) {
            this._dbMin = newMin;
            this._dbMax = newMax;
            // Force LUT rebuild on next drawWaterfall() (colorLUTDbMin/Max check)
            this._colorLUTDbMin = null;
            this._colorLUTDbMax = null;
            console.log(`🌊 Applied waterfall dB range: [${newMin}, ${newMax}] (source: ${this._deviceWaterfallActive ? 'device' : 'global'})`);
        }
    },

    // === Waterfall 3D (Three.js terrain view) ===
    // Lazily toggles between the 2D scroll-blit canvas and the Three.js 3D
    // surface renderer. Both consume the SAME spectrum frames from the WS
    // 'spectrum' handler, so they stay in sync.
    _waterfall3dOn: false,
    toggleWaterfall3D() {
        const btn = document.getElementById('wf-3d-btn');
        const c2d = document.getElementById('waterfall-2d-container');
        const c3d = document.getElementById('waterfall-3d-container');
        const hint = document.getElementById('waterfall-3d-hint');
        if (!c2d || !c3d) return;

        if (this._waterfall3dOn) {
            // → switch back to 2D
            this._waterfall3dOn = false;
            Waterfall3D.dispose();
            c3d.style.display = 'none';
            if (hint) hint.style.display = 'none';
            c2d.style.display = '';
            if (btn) { btn.classList.remove('active'); btn.textContent = '📊 3D'; }
            console.log('📊 Waterfall → 2D');
        } else {
            // → switch to 3D (lazy init)
            c3d.style.display = '';
            if (hint) hint.style.display = '';
            c2d.style.display = 'none';
            const ok = Waterfall3D.init(c3d);
            if (!ok) {
                c3d.style.display = 'none';
                if (hint) hint.style.display = 'none';
                c2d.style.display = '';
                alert(I18n.t('dash.err3dWaterfall'));
                return;
            }
            this._waterfall3dOn = true;
            if (btn) { btn.classList.add('active'); btn.textContent = '📊 2D'; }
            console.log('📊 Waterfall → 3D');
        }
    },

    /** Feed spectrum to the 3D view (no-op when 3D is off). */
    _feedWaterfall3D(spectrum) {
        if (!this._waterfall3dOn) return;
        Waterfall3D.feed(spectrum, this._dbMin, this._dbMax);
        // Signal labels (frequency tags above peaks) — mirrors the 2D Signal
        // Tag feature. Only active when the user has enabled Signal Tag, so
        // the same toggle controls both views and there are no stray labels
        // when the feature is off.
        if (this.signalTagEnabled) {
            const signals = this._detectSignals(spectrum);
            const numBins = spectrum.length;
            const sr = this._sampleRate || 2048000;
            Waterfall3D.setSignalLabels(signals, {
                lowHz: this.currentFreq - sr / 2,
                binHz: sr / numBins,
                numBins,
                sr,
                centerHz: this.currentFreq,
            });
        }
    },

    drawWaterfall(spectrumData) {
        // Accept both typed arrays (Int16Array, Float32Array) and regular arrays
        if (!spectrumData || !spectrumData.length) return;

        // === Auto Range (3 modes: off / once / cont) ===
        // off:  manual — read from localStorage
        // once: compute min/max ONE time, then switch to 'off'
        // cont: continuous tracking (fast rise +1, slow fall -0.1)
        if (this._autoMode === 'once' || this._autoMode === 'cont') {
            let smin = Infinity, smax = -Infinity;
            for (let i = 0; i < spectrumData.length; i++) {
                const v = spectrumData[i];
                if (v < smin) smin = v;
                if (v > smax) smax = v;
            }

            if (this._autoMode === 'once') {
                // === ONCE: compute once (first frame only), then show values ===
                if (this._onceComputed) {
                    // Already computed this cycle; wait for timer to revert to OFF
                } else {
                    let paddedMin = smin - 5;
                    let paddedMax = smax + 5;
                    if (paddedMax - paddedMin < 20) {
                        const mid = (paddedMax + paddedMin) / 2;
                        paddedMin = mid - 10;
                        paddedMax = mid + 10;
                    }
                    this._dbMin = Math.round(paddedMin);
                    this._dbMax = Math.round(paddedMax);
                    this._onceComputed = true;
                    // Save to localStorage so manual mode inherits these values
                    localStorage.setItem('waterfall_db_min', this._dbMin);
                    localStorage.setItem('waterfall_db_max', this._dbMax);
                    // Show computed min/max on the button so user sees the values
                    const btn = document.getElementById('wf-auto-btn');
                    if (btn) {
                        btn.textContent = `${this._dbMin}/${this._dbMax} dB`;
                    }
                    console.log(`🎚️ ONCE done: dB range set to [${this._dbMin}, ${this._dbMax}]`);
                }
            } else {
                // === CONT: continuous tracking with smoothing ===
                // Initialize continuous tracking
                if (this._autoContMin === undefined || this._autoContMax === undefined) {
                    this._autoContMin = smin;
                    this._autoContMax = smax;
                }
                // Fast rise (+1), slow fall (-0.1) — prevents flicker
                if (smax > this._autoContMax + 1) {
                    this._autoContMax += 1;
                } else if (smax < this._autoContMax - 1) {
                    this._autoContMax -= 0.1;
                }
                if (smin < this._autoContMin - 1) {
                    this._autoContMin -= 1;
                } else if (smin > this._autoContMin + 1) {
                    this._autoContMin += 0.1;
                }
                // Add padding and ensure minimum range of 20 dB
                let paddedMin = this._autoContMin - 5;
                let paddedMax = this._autoContMax + 5;
                if (paddedMax - paddedMin < 20) {
                    const mid = (paddedMax + paddedMin) / 2;
                    paddedMin = mid - 10;
                    paddedMax = mid + 10;
                }
                this._dbMin = Math.round(paddedMin);
                this._dbMax = Math.round(paddedMax);
            }
        }

        // Push a copy of the spectrum into pending lines; the render loop
        // will drain them via scroll-blit. Cap pending to canvas height to
        // avoid unbounded growth if the browser stalls.
        const line = new Float32Array(spectrumData);
        const maxPending = this._waterfallCanvasH || 300;
        const speed = this.waterfallSpeed;
        for (let i = 0; i < speed; i++) {
            this._waterfallPendingLines.push(line);
        }
        if (this._waterfallPendingLines.length > maxPending) {
            this._waterfallPendingLines.splice(0, this._waterfallPendingLines.length - maxPending);
        }

        // Signal Tag: detect strong signals and update annotations
        this._updateSignalAnnotations(line);
        // Bookmark signal detection: blink chips/rows green when their
        // frequency has a signal above squelch.
        this._updateBookmarkSignals(line);
    },

    spectrumToColor(db) {
        // Not used anymore - colors computed inline in _renderWaterfall
        return '#000';
    },

    // === Signal Tag: detect signals above squelch ===
    // Returns array of { freq, rightFreq } for each signal region.
    // freq       = peak frequency of the signal (Hz)
    // rightFreq  = right edge frequency (Hz), used for stick X position
    _detectSignals(spectrum) {
        const squelchInput = document.getElementById('squelch-input');
        const squelchLevel = squelchInput ? parseFloat(squelchInput.value) : -70;
        if (!isFinite(squelchLevel)) return [];

        const numBins = spectrum.length;
        if (numBins <= 0) return [];
        const sr = this._sampleRate || 2048000;
        const binHz = sr / numBins;
        const lowHz = this.currentFreq - sr / 2;

        // Minimum width to filter out noise spikes (~0.3% of bins)
        const minWidth = Math.max(2, Math.round(numBins * 0.003));

        const signals = [];
        let regionStart = -1;
        let regionPeak = -1;
        let regionPeakVal = -Infinity;

        for (let i = 0; i <= numBins; i++) {
            const val = (i < numBins) ? spectrum[i] : -Infinity;
            if (val > squelchLevel) {
                if (regionStart < 0) {
                    regionStart = i;
                    regionPeakVal = val;
                    regionPeak = i;
                } else if (val > regionPeakVal) {
                    regionPeakVal = val;
                    regionPeak = i;
                }
            } else if (regionStart >= 0) {
                const width = i - regionStart;
                if (width >= minWidth) {
                    signals.push({
                        freq: Math.round(lowHz + regionPeak * binHz),
                        rightFreq: Math.round(lowHz + i * binHz),
                    });
                }
                regionStart = -1;
                regionPeakVal = -Infinity;
            }
        }
        return signals;
    },

    // === Signal Tag: track annotations and enforce display rules ===
    // Called once per spectrum frame from drawWaterfall().
    //   - Advances existing annotation y positions (scroll with waterfall)
    //   - Removes off-screen annotations
    //   - Detects new signals and creates annotations subject to:
    //       10s cooldown per frequency bucket (25 kHz)
    //       5s stagger for nearby simultaneous signals
    _updateSignalAnnotations(spectrum) {
        if (!this.signalTagEnabled) {
            if (this._signalAnnotations.length > 0) {
                this._signalAnnotations = [];
            }
            return;
        }

        const h = this._waterfallCanvasH || 300;
        const speed = this.waterfallSpeed;
        const now = Date.now();

        // Advance existing annotations downward (scroll with waterfall)
        for (const ann of this._signalAnnotations) {
            ann.y += speed;
            // Activate staggered annotations when their delay expires
            if (!ann.visible && now >= ann.showAtTime) {
                ann.visible = true;
                ann.shownTime = now;
                this._sigTagLastShown[ann.freqKey] = now;
                this._waterfallDirty = true;
            }
        }
        // Remove annotations that have scrolled off-screen
        const before = this._signalAnnotations.length;
        this._signalAnnotations = this._signalAnnotations.filter(a => a.y < h + 60);
        if (this._signalAnnotations.length !== before) this._waterfallDirty = true;

        // Detect signals above squelch threshold
        const signals = this._detectSignals(spectrum);
        if (signals.length === 0) return;

        const COOLDOWN_MS = 10000;      // 10s per-frequency cooldown
        const STAGGER_MS = 5000;        // 5s delay for nearby simultaneous signals
        const FREQ_BUCKET_HZ = 25000;   // frequency grouping for cooldown (~25 kHz)

        for (const sig of signals) {
            const freqKey = Math.round(sig.freq / FREQ_BUCKET_HZ) * FREQ_BUCKET_HZ;

            // Cooldown: same freq bucket can't show again within 10s
            const lastShown = this._sigTagLastShown[freqKey] || 0;
            if (now - lastShown < COOLDOWN_MS) continue;

            // Skip if there's already an annotation (visible or pending) for this freq
            if (this._signalAnnotations.some(a => a.freqKey === freqKey)) continue;

            // Stagger: if a nearby annotation was shown recently, delay this one
            const nearbyRecent = this._signalAnnotations.find(a =>
                a.visible && a.shownTime &&
                Math.abs(a.freq - sig.freq) < FREQ_BUCKET_HZ * 3 &&
                (now - a.shownTime) < STAGGER_MS
            );
            const showAt = nearbyRecent ? (now + STAGGER_MS) : now;

            this._signalAnnotations.push({
                freqKey: freqKey,
                freq: sig.freq,
                rightFreq: sig.rightFreq,
                y: 0,
                showAtTime: showAt,
                visible: (showAt === now),
                shownTime: (showAt === now) ? now : 0,
                colorIdx: this._sigTagColorIdx++,
            });

            if (showAt === now) {
                this._sigTagLastShown[freqKey] = now;
            }
            this._waterfallDirty = true;
        }
    },

    // Shift waterfall back buffer horizontally when frequency changes.
    // deltaHz > 0 (tuned UP): content moves LEFT (signals appear to slide left)
    // deltaHz < 0 (tuned DOWN): content moves RIGHT
    _shiftWaterfallBuffer(deltaHz) {
        // Back buffer is native-bin resolution → shift in bin units.
        const bbW = this._waterfallBins;
        const h = this._waterfallCanvasH;
        if (bbW <= 0 || h <= 0) return;
        const sr = this._sampleRate;
        if (sr <= 0) return;

        // Convert Hz delta to back-buffer pixel (bin) delta
        const deltaPx = Math.round((deltaHz / sr) * bbW);
        if (deltaPx === 0) return;

        const bb = this._waterfallBackBuffer;
        const bbCtx = this._waterfallBackCtx;

        // If shift is larger than the back buffer width, just clear everything
        if (Math.abs(deltaPx) >= bbW) {
            bbCtx.fillStyle = '#0a0a1a';
            bbCtx.fillRect(0, 0, bbW, h);
            this._waterfallDirty = true;
            return;
        }

        if (deltaPx > 0) {
            // Freq went UP → content shifts LEFT
            bbCtx.drawImage(bb, deltaPx, 0, bbW - deltaPx, h, 0, 0, bbW - deltaPx, h);
            bbCtx.fillStyle = '#0a0a1a';
            bbCtx.fillRect(bbW - deltaPx, 0, deltaPx, h);
        } else {
            // Freq went DOWN → content shifts RIGHT
            const absDelta = -deltaPx;
            bbCtx.drawImage(bb, 0, 0, bbW - absDelta, h, absDelta, 0, bbW - absDelta, h);
            bbCtx.fillStyle = '#0a0a1a';
            bbCtx.fillRect(0, 0, absDelta, h);
        }
        this._waterfallDirty = true;
    },

    // === Live Feed ===
    async loadRecentRecordings() {
        try {
            const data = await API.recordings.list({ limit: 20 });
            this.recordings = data.items || data || [];
            this.renderLiveFeed();
        } catch (err) {
            console.error('Failed to load recordings:', err);
        }
    },

    renderLiveFeed() {
        const container = document.getElementById('live-feed-container');
        if (!container) return;
        if (!this.recordings.length) {
            container.innerHTML = `<div class="text-center text-muted p-2" style="font-size:0.8rem;">${I18n.t('dash.noRecordings')}</div>`;
            return;
        }
        container.innerHTML = this.recordings.map(rec => this.createFeedItemHTML(rec)).join('');
    },

    createFeedItemHTML(rec) {
        const ts = rec.recorded_at || rec.timestamp;
        const userTz = getUserTimezone();
        const time = ts ? formatTime(ts, userTz) : '--:--';
        const freqHz = rec.frequency || (rec.frequency_mhz ? rec.frequency_mhz * 1e6 : null);
        const freq = freqHz ? (freqHz / 1e6).toFixed(3) + ' MHz' : (rec.frequency_mhz ? rec.frequency_mhz.toFixed(3) + ' MHz' : '');
        const mode = rec.mode || '';
        const dur = rec.duration ? rec.duration.toFixed(1) + 's' : '';
        const sigVal = rec.signal_strength != null ? rec.signal_strength : rec.signal_level;
        const sig = sigVal != null ? sigVal.toFixed(0) + ' dB' : '';
        const transcript = rec.transcript || rec.transcription || `<span class="text-muted">${I18n.t('dash.transcribing')}</span>`;
        const audioUrl = rec.url || (rec.audio_file_name ? `/recordings/${rec.audio_file_name}` : '');
        const source = rec.source || 'auto';
        const badgeClass = source === 'manual' ? 'manual' : 'auto';
        const badgeLabel = source === 'manual' ? I18n.t('dash.sourceManual') : I18n.t('dash.sourceAuto');
        // Show recorder's name for manual recordings (per-user)
        const recorderName = rec.recorded_by_display_name || rec.recorded_by_username;
        const recorderLabel = (source === 'manual' && recorderName) ? `👤 ${recorderName}` : '';
        // Provider badge (มีไอคอน) — แสดงว่าถอดด้วย STT provider อะไร
        const _PROVIDER_ICONS = { speechmatics: '🔊', azure: '☁️', whisper: '🤫', whisper_local: '🤫' };
        const _PROVIDER_LABELS = { speechmatics: 'Speechmatics', azure: 'Azure', whisper: 'Whisper', whisper_local: 'Whisper' };
        let _providerBadge = '';
        if (rec.stt_provider) {
            const _icon = _PROVIDER_ICONS[rec.stt_provider] || '📝';
            const _label = _PROVIDER_LABELS[rec.stt_provider] || (rec.stt_provider.charAt(0).toUpperCase() + rec.stt_provider.slice(1));
            const _isWhisper = (rec.stt_provider === 'whisper' || rec.stt_provider === 'whisper_local');
            const _model = _isWhisper && rec.stt_model ? ` · ${rec.stt_model}` : '';
            const _dur = (rec.stt_duration != null) ? ` · ⏱ ${Number(rec.stt_duration).toFixed(1)}s` : '';
            _providerBadge = `<span style="font-size:0.65rem; color:var(--text-muted); margin-left:6px; white-space:nowrap;">${_icon} ${_label}${_model}${_dur}</span>`;
        }
        // Delete button (admin only) — icon at the top-right corner of each
        // feed item. Uses a small 🗑 icon to avoid taking horizontal space.
        // stopPropagation prevents the click from triggering the feed item's
        // own click handler (if any).
        const _delBtn = (typeof Auth !== 'undefined' && Auth.isAdmin())
            ? `<button class="feed-del-btn" title="${I18n.t('dash.delFeedTitle')}" onclick="event.stopPropagation(); App.deleteFeedItem('${rec.id}')" style="background:none;border:none;color:var(--text-muted);cursor:pointer;padding:2px 4px;font-size:0.85rem;border-radius:4px;opacity:0.6;transition:opacity 0.15s, color 0.15s, background 0.15s;" onmouseover="this.style.opacity='1';this.style.color='#fca5a5';this.style.background='rgba(239,68,68,0.12)';" onmouseout="this.style.opacity='0.6';this.style.color='var(--text-muted)';this.style.background='none';">🗑</button>`
            : '';

        return `
            <div class="feed-item" id="feed-${rec.id}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span class="feed-time">${time}</span>
                    <div style="display:flex; gap:6px; align-items:center;">
                        <span class="source-badge ${badgeClass}" title="${source === 'manual' ? I18n.t('dash.sourceManualTitle') : I18n.t('dash.sourceAutoTitle')}">${badgeLabel}</span>
                        ${recorderLabel ? `<span style="font-size:0.65rem; color:var(--text-muted); white-space:nowrap;">${recorderLabel}</span>` : ''}
                        <span style="font-size:0.7rem; color:var(--text-secondary);">${freq} ${mode} | ${dur} | ${sig}</span>
                        ${_delBtn}
                    </div>
                </div>
                <div class="feed-text">
                    <div class="transcript" style="font-size:0.8rem;">${transcript}${_providerBadge}</div>
                </div>
                ${audioUrl ? `<audio controls style="width:100%;height:28px;margin-top:4px;" src="${audioUrl}"></audio>` : ''}
            </div>
        `;
    },

    /**
     * Delete a single recording from the dashboard feed.
     * Confirms with the user before deleting both the DB record and the audio
     * file (handled by the backend DELETE /api/recordings/{id} endpoint).
     * Admin only.
     */
    async deleteFeedItem(recId) {
        if (typeof Auth === 'undefined' || !Auth.isAdmin()) return;
        if (!confirm(I18n.t('dash.delFeedConfirm'))) return;
        try {
            await API.recordings.delete(recId);
            // Remove from local data + re-render
            this.recordings = this.recordings.filter(r => r.id !== recId);
            const el = document.getElementById('feed-' + recId);
            if (el) el.remove();
            console.log('🗑 Deleted feed item:', recId);
        } catch (err) {
            console.error('Delete feed item failed:', err);
            alert(I18n.t('dash.delFail') + (err.detail || err.message || err));
        }
    },

    addLiveFeedItem(data) {
        this.recordings.unshift(data);
        if (this.recordings.length > 20) this.recordings.pop();
        this.renderLiveFeed();
    },

    viewAllRecordings() {
        window.location.href = '/recordings';
    },

    // === Bookmarks (client-side, per-user via localStorage) ===
    // Each bookmark: { id, name, freqHz, mode, createdAt }
    // Key is namespaced by username so different accounts on the same
    // browser don't see each other's bookmarks.
    _BOOKMARK_MODES: ['FM', 'AM', 'USB', 'LSB'],

    _bookmarkKey() {
        let user = '';
        try { user = (Auth.getUser() || {}).username || 'guest'; } catch (e) { user = 'guest'; }
        return `bookmarks_v1__${user}`;
    },

    _loadBookmarks() {
        try {
            const raw = localStorage.getItem(this._bookmarkKey());
            this.bookmarks = raw ? JSON.parse(raw) : [];
            if (!Array.isArray(this.bookmarks)) this.bookmarks = [];
        } catch (e) {
            console.warn('Failed to load bookmarks:', e);
            this.bookmarks = [];
        }
        return this.bookmarks;
    },

    _saveBookmarks() {
        try {
            localStorage.setItem(this._bookmarkKey(), JSON.stringify(this.bookmarks));
        } catch (e) {
            console.error('Failed to save bookmarks:', e);
            alert(I18n.t('dash.bmSaveFail'));
        }
    },

    /** Initialize bookmark UI: load from storage + render both panels + wire
     *  the collapse toggle for the right-panel frequency list. */
    initBookmarks() {
        this._loadBookmarks();
        // Restore the collapsed state of the right-panel frequency list.
        const freqList = document.querySelector('.freq-list');
        if (freqList) {
            const collapsed = localStorage.getItem('freq_list_collapsed') === 'true';
            freqList.classList.toggle('collapsed', collapsed);
            const chev = document.getElementById('freq-list-chevron');
            if (chev) chev.textContent = collapsed ? '▸' : '▾';
        }
        this.renderBookmarks();
        this.renderFreqList();
    },

    /** Bookmark chips in the LEFT panel (Bookmarks bar). Clicking a chip
     *  instantly tunes the active VFO to the bookmark's frequency. */
    renderBookmarks() {
        const container = document.getElementById('bookmarks-container');
        if (!container) return;
        if (!this.bookmarks.length) {
            container.innerHTML = `<div class="text-muted" style="font-size:0.75rem;padding:6px 8px;">${I18n.t('dash.noBookmark')}</div>`;
            return;
        }
        container.innerHTML = this.bookmarks.map(bm => {
            const mhz = (bm.freqHz / 1e6).toFixed(4);
            const escName = this._esc(bm.name);
            return `<button class="bookmark-chip" data-bm-id="${bm.id}" title="${escName} — ${mhz} MHz (${bm.mode || 'FM'})"
                        onclick="App.gotoBookmark('${bm.id}')">
                        <span class="bookmark-chip-name">${escName}</span>
                        <span class="bookmark-chip-freq">${mhz}</span>
                    </button>`;
        }).join('');
    },

    /** Rows in the RIGHT panel ("ความถี่ที่บันทึกไว้"). Each row shows
     *  name + frequency and is clickable. Also has small edit/delete actions. */
    renderFreqList() {
        const container = document.getElementById('freq-list-container');
        const count = document.getElementById('freq-count');
        if (count) count.textContent = this.bookmarks.length;
        if (!container) return;
        if (!this.bookmarks.length) {
            container.innerHTML = `<div class="text-muted" style="font-size:0.75rem;padding:6px 12px;">${I18n.t('dash.noSavedFreq')}</div>`;
            return;
        }
        // Sort by frequency ascending for easy scanning.
        const sorted = [...this.bookmarks].sort((a, b) => a.freqHz - b.freqHz);
        container.innerHTML = sorted.map(bm => {
            const mhz = (bm.freqHz / 1e6).toFixed(4);
            const escName = this._esc(bm.name);
            const mode = bm.mode || 'FM';
            return `<div class="freq-item" data-bm-id="${bm.id}" onclick="App.gotoBookmark('${bm.id}')" title="${I18n.t('dash.bmGotoTitle', escName, mhz)}">
                        <span class="signal-led"></span>
                        <div class="freq-info">
                            <div class="freq-value">${mhz} <span class="freq-unit">MHz</span></div>
                            <div class="freq-name">${escName}</div>
                        </div>
                        <span class="freq-mode">${mode}</span>
                        <span class="bm-actions">
                            <button class="bm-act-btn" title="${I18n.t('dash.bmEditTitle2')}" onclick="event.stopPropagation(); App.editBookmark('${bm.id}')">✏️</button>
                            <button class="bm-act-btn" title="${I18n.t('dash.bmDeleteTitle')}" onclick="event.stopPropagation(); App.deleteBookmark('${bm.id}')">🗑️</button>
                        </span>
                    </div>`;
        }).join('');
    },

    // === Bookmark signal detection ===
    // Called every spectrum frame from drawWaterfall(). For each bookmark
    // whose frequency falls inside the current passband, we sample the dB at
    // that frequency and compare it to the squelch threshold. If above SQL,
    // the corresponding chip + row get the `.signal-active` class so they
    // flash green via CSS. A short hold (~400ms) keeps the flash lit through
    // brief noise dips so the UI doesn't strobe.
    _bmSignalUntil: {},   // bmId → timestamp until which the flash stays on
    _updateBookmarkSignals(spectrum) {
        if (!this.bookmarks.length || !spectrum || !spectrum.length) return;

        const numBins = spectrum.length;
        const sr = this._sampleRate || 2048000;
        const binHz = sr / numBins;
        const lowHz = this.currentFreq - sr / 2;
        const highHz = this.currentFreq + sr / 2;

        // Read the current squelch threshold.
        const squelchInput = document.getElementById('squelch-input');
        const squelchLevel = squelchInput ? parseFloat(squelchInput.value) : -120;
        const sql = isFinite(squelchLevel) ? squelchLevel : -120;

        const now = Date.now();
        const HOLD_MS = 400;  // keep flash on through brief dips

        for (const bm of this.bookmarks) {
            const f = bm.freqHz;
            // Skip bookmarks outside the current passband (can't measure).
            if (f < lowHz || f > highHz) {
                // Make sure a previously-active state is cleared.
                this._setBookmarkSignal(bm.id, false);
                continue;
            }
            // Map frequency → bin center. Average ±1 bin to reject spikes.
            const bin = Math.round((f - lowHz) / binHz);
            let sum = 0, cnt = 0;
            for (let k = bin - 1; k <= bin + 1; k++) {
                if (k >= 0 && k < numBins) { sum += spectrum[k]; cnt++; }
            }
            const db = cnt > 0 ? (sum / cnt) : -Infinity;

            const active = db > sql;
            if (active) {
                this._bmSignalUntil[bm.id] = now + HOLD_MS;
            }
            const on = active || (this._bmSignalUntil[bm.id] || 0) > now;
            this._setBookmarkSignal(bm.id, on);
        }
    },

    /** Apply/remove the signal-active flash on a bookmark's chip + row. */
    _setBookmarkSignal(bmId, on) {
        const chip = document.querySelector(`.bookmark-chip[data-bm-id="${bmId}"]`);
        if (chip) chip.classList.toggle('signal-active', on);
        const row = document.querySelector(`.freq-item[data-bm-id="${bmId}"]`);
        if (row) {
            row.classList.toggle('signal-active', on);
            const led = row.querySelector('.signal-led');
            if (led) led.classList.toggle('on', on);
        }
    },

    /** Tune the active VFO to a bookmark (and switch mode if needed). */
    gotoBookmark(id) {
        const bm = this.bookmarks.find(b => b.id === id);
        if (!bm) return;
        this.setVFO(bm.freqHz);
        // Switch demod mode if the bookmark carries one and it differs.
        if (bm.mode && bm.mode !== this.currentMode) {
            const btn = document.querySelector(`.mode-btn[data-mode="${bm.mode}"]`);
            if (btn) btn.click();
        }
        console.log(`🔖 → ${bm.name} @ ${(bm.freqHz/1e6).toFixed(4)} MHz`);
    },

    /** Add: opens a dialog pre-filled with the CURRENT active-VFO frequency. */
    showAddBookmark() {
        const vfoHz = this.currentFreq + (this._vfoOffsetHz || 0);
        this._openBookmarkDialog({
            mode: 'add',
            name: '',
            freqHz: vfoHz,
            demodMode: this.currentMode,
        });
    },

    editBookmark(id) {
        const bm = this.bookmarks.find(b => b.id === id);
        if (!bm) return;
        this._openBookmarkDialog({
            mode: 'edit',
            id: id,
            name: bm.name,
            freqHz: bm.freqHz,
            demodMode: bm.mode || this.currentMode,
        });
    },

    deleteBookmark(id) {
        const bm = this.bookmarks.find(b => b.id === id);
        if (!bm) return;
        if (!confirm(I18n.t('dash.bmDeleteConfirm', bm.name))) return;
        this.bookmarks = this.bookmarks.filter(b => b.id !== id);
        this._saveBookmarks();
        this.renderBookmarks();
        this.renderFreqList();
        console.log('🔖 Deleted bookmark:', bm.name);
    },

    /** Shared dialog for add/edit. Pre-fills frequency from the active VFO,
     *  but the user can freely change name / frequency / mode. */
    _openBookmarkDialog({ mode, id, name, freqHz, demodMode }) {
        const isEdit = mode === 'edit';
        const overlay = document.createElement('div');
        overlay.className = 'freq-input-overlay';
        const modeOptions = this._BOOKMARK_MODES.map(m =>
            `<option value="${m}" ${m === demodMode ? 'selected' : ''}>${m}</option>`
        ).join('');
        overlay.innerHTML = `
            <div class="freq-input-box bm-dialog">
                <h3>${isEdit ? I18n.t('dash.bmEditTitle') : I18n.t('dash.bmAddTitle')}</h3>
                <label class="bm-label">${I18n.t('dash.bmName')}</label>
                <input type="text" id="bm-name" value="${this._esc(name)}" placeholder="${I18n.t('dash.bmNamePh')}" autofocus>
                <label class="bm-label">${I18n.t('dash.bmFreq')}</label>
                <input type="text" id="bm-freq" value="${(freqHz/1e6).toFixed(4)}">
                <label class="bm-label">${I18n.t('dash.bmMode')}</label>
                <select id="bm-mode">${modeOptions}</select>
                <div class="freq-input-btns">
                    <button class="btn btn-primary" id="bm-ok">${isEdit ? I18n.t('dash.bmSave') : I18n.t('dash.bmAddBtn')}</button>
                    <button class="btn" id="bm-cancel">${I18n.t('dash.bmCancel')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const nameEl = document.getElementById('bm-name');
        const freqEl = document.getElementById('bm-freq');
        nameEl.focus();
        nameEl.select();

        const close = () => overlay.remove();
        const apply = () => {
            const nm = nameEl.value.trim();
            const fMHz = parseFloat(freqEl.value);
            const md = document.getElementById('bm-mode').value;
            if (!nm) { alert(I18n.t('dash.bmNameRequired')); nameEl.focus(); return; }
            if (isNaN(fMHz) || fMHz < 0.5 || fMHz > 1766) {
                alert(I18n.t('dash.bmFreqRange'));
                freqEl.focus();
                return;
            }
            const record = {
                id: id || ('bm_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7)),
                name: nm,
                freqHz: Math.round(fMHz * 1e6),
                mode: md,
                createdAt: isEdit ? (this.bookmarks.find(b => b.id === id) || {}).createdAt || Date.now() : Date.now(),
            };
            if (isEdit) {
                const idx = this.bookmarks.findIndex(b => b.id === id);
                if (idx >= 0) this.bookmarks[idx] = record;
            } else {
                this.bookmarks.push(record);
            }
            this._saveBookmarks();
            this.renderBookmarks();
            this.renderFreqList();
            close();
            console.log(`🔖 ${isEdit ? 'Updated' : 'Added'} bookmark:`, record.name);
        };

        document.getElementById('bm-ok').addEventListener('click', apply);
        document.getElementById('bm-cancel').addEventListener('click', close);
        [nameEl, freqEl].forEach(el => el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') apply();
            if (e.key === 'Escape') close();
        }));
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    },

    /** Toggle collapse of the right-panel frequency list. Persisted. */
    toggleFreqListCollapse() {
        const freqList = document.querySelector('.freq-list');
        if (!freqList) return;
        const collapsed = freqList.classList.toggle('collapsed');
        const chev = document.getElementById('freq-list-chevron');
        if (chev) chev.textContent = collapsed ? '▸' : '▾';
        localStorage.setItem('freq_list_collapsed', String(collapsed));
    },

    /** Minimal HTML escaper for user-provided bookmark names.
     *  Entity strings are built at runtime via charCode so that no HTML
     *  entities appear literally in the source (some tooling decodes them). */
    _esc(s) {
        const A = String.fromCharCode(38); // ampersand char
        const map = {};
        map[String.fromCharCode(38)] = A + 'amp;';   // &  -> &
        map[String.fromCharCode(60)] = A + 'lt;';    // <  -> <
        map[String.fromCharCode(62)] = A + 'gt;';    // >  -> >
        map[String.fromCharCode(34)] = A + 'quot;';  // "  -> "
        map[String.fromCharCode(39)] = A + '#039;';  // '  -> &#039;
        return String(s == null ? '' : s).replace(/[&<>"']/g, (ch) => map[ch] || ch);
    },
};

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});

// Re-render dynamic content when language changes
document.addEventListener('languagechange', () => {
    if (typeof App !== 'undefined' && App.recordings) {
        App.renderLiveFeed();
        App.renderBookmarks();
        App.renderFreqList();
    }
});
