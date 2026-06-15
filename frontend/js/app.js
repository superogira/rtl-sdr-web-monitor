/**
 * Main Application - RTL-SDR Monitor Dashboard
 */
const App = {
    VERSION: '2024.06.14-v7',  // UI fixes: mobile collapse pos + auto click/hold
    currentFreq: 145500000,  // Hz
    currentMode: 'FM',
    currentSignal: -100,     // dBm
    bookmarks: [],
    recordings: [],
    isRecording: false,
    isConnected: false,
    isStreaming: false,

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

        this.startClock();
        this.setupModeSelector();
        this.setupRadioControls();
        this.initWaterfall();

        // Sync state from backend first
        await this.syncState();

        WS.connect();
        this.setupWSListeners();

        // Send saved FFT size to backend
        const fftSize = parseInt(localStorage.getItem('fft_size') || '1024');
        API.sdr.setFFTSize(fftSize).catch(() => {});

        await this.loadRecentRecordings();
        this._showVersionBadge();
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

    _showVersionBadge() {
        // Display version so users can confirm they have the latest code.
        let el = document.getElementById('app-version');
        if (!el) {
            el = document.createElement('span');
            el.id = 'app-version';
            el.style.cssText = 'font-size:0.6rem;color:var(--text-muted);padding:0 6px;opacity:0.7;white-space:nowrap;';
            // Insert into header-right (near settings/logout)
            const headerRight = document.querySelector('.header-right');
            if (headerRight) {
                headerRight.insertBefore(el, headerRight.firstChild);
            }
        }
        el.textContent = 'v' + this.VERSION;
    },

    async syncState() {
        try {
            const status = await API.sdr.status();
            this.isConnected = status.connected || false;
            this.isStreaming = status.streaming || false;
            this.currentFreq = status.frequency || 145500000;
            this.currentMode = status.mode || 'FM';
            this.currentSignal = status.signal_level_db || -120;
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
        const statusLed = document.getElementById('connection-status');

        if (this.isConnected) {
            if (btnConnect) { btnConnect.style.display = 'none'; }
            if (btnStream) { btnStream.style.display = ''; }
            if (statusLed) { statusLed.style.background = 'var(--led-green)'; statusLed.style.boxShadow = '0 0 6px var(--led-green)'; }
        } else {
            if (btnConnect) { btnConnect.style.display = ''; btnConnect.textContent = '🔗 เชื่อมต่อ'; btnConnect.disabled = false; }
            if (btnStream) { btnStream.style.display = 'none'; }
            if (statusLed) { statusLed.style.background = 'var(--led-red)'; statusLed.style.boxShadow = 'none'; }
        }

        if (this.isStreaming) {
            if (btnStream) { btnStream.textContent = '⏹ หยุดรับ'; }
        } else {
            if (btnStream) { btnStream.textContent = '▶ รับสัญญาณ'; }
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
        // Sync channel bandwidth button active state (buttons in header)
        if (data.channel_bw !== undefined) {
            this._channelBw = data.channel_bw;
            const bwButtons = document.getElementById('bw-buttons');
            if (bwButtons) {
                bwButtons.querySelectorAll('.bw-btn').forEach(b =>
                    b.classList.toggle('active', parseInt(b.dataset.bw) === data.channel_bw)
                );
            }
        }
        // Sync gain slider
        if (data.gain !== undefined) {
            const gs = document.getElementById('gain-slider');
            const gv = document.getElementById('gain-value');
            if (gs) gs.value = data.gain;
            if (gv) gv.textContent = data.gain + ' dB';
        }
        // Sync squelch slider
        if (data.squelch_level !== undefined) {
            const ss = document.getElementById('squelch-slider');
            const sv = document.getElementById('squelch-value');
            if (ss) ss.value = data.squelch_level;
            if (sv) sv.textContent = data.squelch_level + ' dB';
        }
    },

    // === SDR Connection ===

    async connectSDR() {
        const btn = document.getElementById('btn-connect');
        btn.textContent = '⏳ กำลังเชื่อมต่อ...';
        btn.disabled = true;

        try {
            const result = await API.sdr.connect();
            this.isConnected = true;
            btn.textContent = '✅ เชื่อมต่อแล้ว';
            btn.style.display = 'none';
            document.getElementById('btn-stream').style.display = '';

            const status = document.getElementById('connection-status');
            if (status) { status.style.background = 'var(--led-green)'; status.style.boxShadow = '0 0 6px var(--led-green)'; }
        } catch (err) {
            btn.textContent = '🔗 เชื่อมต่อ';
            btn.disabled = false;
            alert('ไม่สามารถเชื่อมต่อ RTL-TCP ได้: ' + (err.detail || err.message || err));
        }
    },

    async toggleStream() {
        const btn = document.getElementById('btn-stream');
        if (this.isStreaming) {
            try { await API.sdr.streamStop(); } catch(e) {}
            this.isStreaming = false;
            btn.textContent = '▶ รับสัญญาณ';
        } else {
            try {
                await API.sdr.streamStart();
                this.isStreaming = true;
                btn.textContent = '⏹ หยุดรับ';
            } catch (err) {
                alert('ไม่สามารถเริ่มรับสัญญาณ: ' + (err.detail || err.message || err));
            }
        }
    },

    // === Clock ===
    startClock() {
        const thaiMonths = ['มกราคม','กุมภาพันธ์','มีนาคม','เมษายน','พฤษภาคม','มิถุนายน','กรกฎาคม','สิงหาคม','กันยายน','ตุลาคม','พฤศจิกายน','ธันวาคม'];
        const thaiDays = ['วันอาทิตย์','วันจันทร์','วันอังคาร','วันพุธ','วันพฤหัสบดี','วันศุกร์','วันเสาร์'];
        const update = () => {
            const now = new Date();
            const h = String(now.getHours()).padStart(2,'0');
            const m = String(now.getMinutes()).padStart(2,'0');
            const s = String(now.getSeconds()).padStart(2,'0');
            const ct = document.getElementById('clock-time');
            const cd = document.getElementById('clock-date');
            const cdy = document.getElementById('clock-day');
            if (ct) ct.textContent = `${h}:${m}:${s}`;
            if (cd) cd.textContent = `${now.getDate()} ${thaiMonths[now.getMonth()]} ${now.getFullYear()+543}`;
            if (cdy) cdy.textContent = thaiDays[now.getDay()];
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
            try { await API.sdr.setMode(mode); } catch (err) { console.error('Set mode failed:', err); }
        });
    },

    // === Radio Controls ===
    _stepSize: 25000,  // Default step: 25 kHz
    _sampleRate: 2048000,  // Default: 2.048 MSPS
    _channelBw: 12000,  // Default channel BW: 12 kHz (NBFM voice)

    setupRadioControls() {
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

        // Direct frequency input (click on freq display)
        const freqBox = document.getElementById('freq-display-box');
        if (freqBox) freqBox.addEventListener('click', () => this.showFreqInput());

        // Gain slider
        const gainSlider = document.getElementById('gain-slider');
        if (gainSlider) {
            gainSlider.addEventListener('input', (e) => {
                document.getElementById('gain-value').textContent = e.target.value + ' dB';
            });
            gainSlider.addEventListener('change', (e) => {
                API.sdr.setGain(parseInt(e.target.value)).catch(err => console.error('Gain failed:', err));
            });
        }

        // Squelch slider
        const squelchSlider = document.getElementById('squelch-slider');
        if (squelchSlider) {
            squelchSlider.addEventListener('input', (e) => {
                document.getElementById('squelch-value').textContent = e.target.value + ' dB';
            });
            squelchSlider.addEventListener('change', (e) => {
                API.sdr.setSquelch(parseFloat(e.target.value)).catch(err => console.error('Squelch failed:', err));
            });
        }

        // Record toggle button
        const btnRecord = document.getElementById('btn-record');
        if (btnRecord) btnRecord.addEventListener('click', () => this.toggleRecording());

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

    tuneFreq(delta) {
        this.currentFreq += delta;
        if (this.currentFreq < 24000000) this.currentFreq = 24000000;  // 24 MHz min
        if (this.currentFreq > 1766000000) this.currentFreq = 1766000000;  // 1.766 GHz max
        this.updateFreqDisplay();
        API.sdr.setFrequency(this.currentFreq).catch(err => console.error('Tune failed:', err));
    },

    showFreqInput() {
        const currentMHz = (this.currentFreq / 1e6).toFixed(4);
        const overlay = document.createElement('div');
        overlay.className = 'freq-input-overlay';
        overlay.innerHTML = `
            <div class="freq-input-box">
                <h3>📻 ใส่ความถี่ (MHz)</h3>
                <input type="text" id="freq-input-field" value="${currentMHz}" autofocus>
                <div class="freq-input-btns">
                    <button class="btn btn-primary" id="freq-input-ok">✅ ตั้งค่า</button>
                    <button class="btn" id="freq-input-cancel">ยกเลิก</button>
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
            if (!isNaN(val) && val >= 24 && val <= 1766) {
                this.currentFreq = Math.round(val * 1e6);
                this.updateFreqDisplay();
                API.sdr.setFrequency(this.currentFreq).catch(err => console.error('Tune failed:', err));
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

    updateFreqDisplay() {
        // === Shift waterfall back buffer when frequency changes ===
        // This makes existing waterfall lines scroll horizontally, so you
        // can see where signals are relative to the new center frequency.
        if (this._waterfallBackBuffer && this._lastWaterfallFreq !== undefined) {
            const deltaHz = this.currentFreq - this._lastWaterfallFreq;
            if (Math.abs(deltaHz) > 0) {
                this._shiftWaterfallBuffer(deltaHz);
            }
        }
        this._lastWaterfallFreq = this.currentFreq;

        const freqDisplay = document.getElementById('freq-display');
        if (!freqDisplay) return;
        const mhz = this.currentFreq / 1e6;
        const parts = mhz.toFixed(4).split('.');
        const intPart = parts[0].split('').join(' ');
        const decPart = parts[1];
        // Show all 4 decimal digits
        freqDisplay.innerHTML = `${intPart} . ${decPart.slice(0,3)} <span class="decimal">${decPart.slice(3)}</span>`;

        // Update waterfall overlay - 4 decimal places
        const freqCenter = document.getElementById('freq-center');
        if (freqCenter) freqCenter.textContent = `${mhz.toFixed(4)} MHz`;

        // Compute freq-low and freq-high from center ± half sample rate
        const halfBw = this._sampleRate / 2;
        const lowMhz = (this.currentFreq - halfBw) / 1e6;
        const highMhz = (this.currentFreq + halfBw) / 1e6;
        const freqLow = document.getElementById('freq-low');
        const freqHigh = document.getElementById('freq-high');
        if (freqLow) freqLow.textContent = `${lowMhz.toFixed(4)} MHz`;
        if (freqHigh) freqHigh.textContent = `${highMhz.toFixed(4)} MHz`;
    },

    async toggleRecording() {
        const btn = document.getElementById('btn-record');
        // Guard: prevent manual record if not streaming
        if (!this.isRecording && !this.isStreaming) {
            alert('กรุณากด "▶ รับสัญญาณ" ก่อน แล้วค่อยกดบันทึก');
            return;
        }
        try {
            if (this.isRecording) {
                const result = await API.sdr.recordStop();
                // Always reset state after stop attempt (even if buffer was empty)
                this.isRecording = false;
                if (btn) btn.classList.remove('recording');
                if (result.note) {
                    console.log('⏹ Manual recording stopped (no audio captured)');
                } else {
                    console.log('⏹ Manual recording stopped');
                }
            } else {
                await API.sdr.recordStart();
                this.isRecording = true;
                if (btn) btn.classList.add('recording');
                console.log('⏺ Manual recording started');
            }
        } catch (err) {
            // Force reset state — backend error means we're not recording
            this.isRecording = false;
            if (btn) btn.classList.remove('recording');
            const msg = err.detail || err.message || err;
            console.error('Recording toggle failed:', msg);
            alert('ไม่สามารถบันทึกได้: ' + msg);
        }
    },

    async _syncRecordingState() {
        try {
            const status = await API.sdr.status();
            // Only sync MANUAL recording state (auto runs independently)
            this.isRecording = !!status.manual_recording;
            const btn = document.getElementById('btn-record');
            if (btn) btn.classList.toggle('recording', this.isRecording);
            // Show auto-recording indicator if active
            this.updateAutoRecordIndicator(status.auto_recording, status.recording_source);
        } catch (e) { /* ignore */ }
    },

    updateAutoRecordIndicator(isAuto, source) {
        let el = document.getElementById('auto-rec-indicator');
        if (isAuto) {
            if (!el) {
                el = document.createElement('span');
                el.id = 'auto-rec-indicator';
                el.style.cssText = 'font-size:0.7rem;color:var(--accent-yellow);margin-left:4px;animation:blink 1s infinite;';
                const btn = document.getElementById('btn-record');
                if (btn && btn.parentNode) btn.parentNode.insertBefore(el, btn.nextSibling);
            }
            el.textContent = '🔄AUTO';
            el.title = `กำลังอัดอัตโนมัติ (source: ${source || 'auto'})`;
        } else if (el) {
            el.remove();
        }
    },

    // === Volume ===
    initVolumeSlider() {
        const slider = document.getElementById('volume-slider');
        const pct = document.getElementById('volume-pct');
        const icon = document.getElementById('volume-icon');
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

        slider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value);
            if (pct) pct.textContent = val + '%';
            this._updateVolumeIcon(val, icon);
            if (typeof LiveAudio !== 'undefined') {
                LiveAudio.setVolume(val / 100);
            }
        });
    },

    _updateVolumeIcon(pct, icon) {
        if (!icon) return;
        if (pct === 0) icon.textContent = '🔇';
        else if (pct < 33) icon.textContent = '🔈';
        else if (pct < 66) icon.textContent = '🔉';
        else icon.textContent = '🔊';
    },

    // === Signal Meter ===
    updateSignalMeter(dbm) {
        this.currentSignal = dbm;
        const bars = document.querySelectorAll('#signal-bars .bar');
        const dbmDisplay = document.getElementById('signal-dbm');
        if (!bars.length) return;
        const normalized = Math.max(0, Math.min(10, (dbm + 120) / 9));
        const activeBars = Math.round(normalized);
        bars.forEach((bar, i) => {
            bar.className = 'bar';
            if (i < activeBars) {
                if (i < 5) bar.classList.add('active-green');
                else if (i < 8) bar.classList.add('active-yellow');
                else bar.classList.add('active-red');
            }
        });
        if (dbmDisplay) {
            dbmDisplay.textContent = `${dbm.toFixed(0)} dBm`;
            dbmDisplay.style.color = activeBars > 6 ? 'var(--accent-yellow)' : 'var(--accent-green)';
        }
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
                this.isStreaming = data.streaming;
                changed = true;
            }
            if (data.signal_level_db !== undefined) {
                this.updateSignalMeter(data.signal_level_db);
            }
            if (data.frequency) {
                this.currentFreq = data.frequency;
                this.updateFreqDisplay();
            }
            if (data.mode) {
                this.currentMode = data.mode;
                document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === data.mode));
                const rm = document.getElementById('radio-mode');
                if (rm) rm.textContent = data.mode;
            }
            // Sync sliders + SRATE button from backend (single source of truth)
            this.syncControlsFromStatus(data);
            // Sync listener count (live audio)
            if (data.listeners !== undefined) {
                const el = document.getElementById('listener-count');
                if (el) {
                    el.textContent = data.listeners > 0 ? `👥 ${data.listeners}` : '';
                }
            }
            // Sync auto-recording indicator (independent from manual record button)
            if (data.auto_recording !== undefined || data.recording_source !== undefined) {
                this.updateAutoRecordIndicator(data.auto_recording, data.recording_source);
            }
            if (changed) {
                this.updateUI();
            }
        });

        WS.on('spectrum', (data) => {
            // Decode base64 int16 binary (Phase 2: efficient transfer)
            let spectrum;
            if (typeof data === 'string') {
                // Binary mode: base64 → int16 array
                const binary = atob(data);
                const len = binary.length / 2;
                spectrum = new Int16Array(len);
                for (let i = 0; i < len; i++) {
                    spectrum[i] = (binary.charCodeAt(i*2) | (binary.charCodeAt(i*2+1) << 8)) << 16 >> 16;
                }
            } else if (Array.isArray(data)) {
                // Legacy JSON mode (backwards compatible)
                spectrum = data;
            } else {
                return;
            }
            this.drawWaterfall(spectrum);
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
                // PRESERVE existing waterfall content on resize (don't clear!)
                const oldW = this._waterfallCanvasW;
                const oldH = this._waterfallCanvasH;
                let oldCanvas = null;
                if (oldW > 0 && oldH > 0 && this._waterfallBackBuffer) {
                    oldCanvas = document.createElement('canvas');
                    oldCanvas.width = oldW;
                    oldCanvas.height = oldH;
                    oldCanvas.getContext('2d').drawImage(this._waterfallBackBuffer, 0, 0);
                }
                this._waterfallCanvasW = w;
                this._waterfallCanvasH = h;
                canvas.width = w;
                canvas.height = h;
                this._waterfallBackBuffer.width = w;
                this._waterfallBackBuffer.height = h;
                const bb = this._waterfallBackCtx;
                // Fill background
                bb.fillStyle = '#0a0a1a';
                bb.fillRect(0, 0, w, h);
                // Restore old content (top-left aligned)
                if (oldCanvas) {
                    bb.drawImage(oldCanvas, 0, 0);
                }
                this._waterfallLineBins = 0;  // force rebuild of line canvas/img
                this._waterfallDirty = true;
            }
        };

        if (window.ResizeObserver && canvas.parentElement) {
            new ResizeObserver(resize).observe(canvas.parentElement);
        }
        window.addEventListener('resize', resize);
        requestAnimationFrame(resize);

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
        // Read live settings from localStorage (only when NOT in auto mode)
        if (this._autoMode === 'off') {
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
        const pending = this._waterfallPendingLines;
        if (pending.length > 0) {
            const numNew = Math.min(pending.length, h);
            // Scroll existing content DOWN by numNew pixels via drawImage self-blit.
            if (numNew < h) {
                bbCtx.drawImage(bb, 0, 0, w, h - numNew, 0, numNew, w, h - numNew);
            }
            // Clear the top numNew rows
            bbCtx.fillStyle = '#0a0a1a';
            bbCtx.fillRect(0, 0, w, numNew);

            // Setup line canvas at numBins width (for bilinear scaling)
            const firstLine = pending[pending.length - 1];
            const numBins = firstLine.length;
            if (numBins <= 0) {
                pending.length = 0;
                return;
            }
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

            // Enable smoothing on back buffer for bilinear scaling (Fix A)
            bbCtx.imageSmoothingEnabled = true;
            bbCtx.imageSmoothingQuality = 'high';

            for (let i = 0; i < numNew; i++) {
                const line = pending[pending.length - numNew + i];
                const y = numNew - 1 - i;

                // Render spectrum into ImageData at numBins resolution (native FFT bins)
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

                // drawImage with scale → browser does bilinear interpolation (Fix A)
                // Vertical overlap blend (Fix B): draw with alpha 0.85 at top edge
                // to blend with existing content, producing smoother vertical transitions
                bbCtx.globalAlpha = 0.85;
                bbCtx.drawImage(lineCanvas, 0, 0, numBins, 1, 0, y, w, 1);
                bbCtx.globalAlpha = 1.0;
            }
            pending.length = 0;
            this._waterfallDirty = true;
        }

        // Composite to main canvas only when dirty, then draw grid + overlays.
        if (this._waterfallDirty) {
            ctx.drawImage(bb, 0, 0);
            this._drawFreqGrid(ctx, w, h);
            this._drawChannelBwOverlay(ctx, w, h);
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
    _drawChannelBwOverlay(ctx, w, h) {
        const bw = this._channelBw || 12000;
        const sr = this._sampleRate || 2048000;
        const bwPx = Math.min(w, (bw / sr) * w);
        if (bwPx < 4) return;  // too small to draw

        const cx = w / 2;
        const left = cx - bwPx / 2;
        const right = cx + bwPx / 2;

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

    _drawFreqGrid(ctx, w, h) {
        const centerHz = this.currentFreq;
        const sr = this._sampleRate || 2048000;
        const lowHz = centerHz - sr / 2;
        const highHz = centerHz + sr / 2;
        const bwHz = highHz - lowHz;

        // Choose "nice" tick interval based on bandwidth
        // Target ~6-10 grid lines across the width
        const targetCount = 8;
        const rawStep = bwHz / targetCount;
        const steps = [10000, 25000, 50000, 100000, 250000, 500000, 1000000, 2500000, 5000000, 10000000];
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

    // Waterfall speed: how many lines to push per spectrum frame
    get waterfallSpeed() {
        return parseInt(localStorage.getItem('waterfall_speed') || '1');
    },
    set waterfallSpeed(val) {
        localStorage.setItem('waterfall_speed', val);
    },

    // Waterfall dB range
    get waterfallDbMin() {
        return parseInt(localStorage.getItem('waterfall_db_min') || '-60');
    },
    set waterfallDbMin(val) {
        localStorage.setItem('waterfall_db_min', val);
        this._dbMin = val;
    },
    get waterfallDbMax() {
        return parseInt(localStorage.getItem('waterfall_db_max') || '5');
    },
    set waterfallDbMax(val) {
        localStorage.setItem('waterfall_db_max', val);
        this._dbMax = val;
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
    },

    spectrumToColor(db) {
        // Not used anymore - colors computed inline in _renderWaterfall
        return '#000';
    },

    // Shift waterfall back buffer horizontally when frequency changes.
    // deltaHz > 0 (tuned UP): content moves LEFT (signals appear to slide left)
    // deltaHz < 0 (tuned DOWN): content moves RIGHT
    _shiftWaterfallBuffer(deltaHz) {
        const w = this._waterfallCanvasW;
        const h = this._waterfallCanvasH;
        if (w <= 0 || h <= 0) return;
        const sr = this._sampleRate;
        if (sr <= 0) return;

        // Convert Hz delta to pixel delta
        const deltaPx = Math.round((deltaHz / sr) * w);
        if (deltaPx === 0) return;

        const bb = this._waterfallBackBuffer;
        const bbCtx = this._waterfallBackCtx;

        // If shift is larger than canvas width, just clear everything
        if (Math.abs(deltaPx) >= w) {
            bbCtx.fillStyle = '#0a0a1a';
            bbCtx.fillRect(0, 0, w, h);
            this._waterfallDirty = true;
            return;
        }

        if (deltaPx > 0) {
            // Freq went UP → content shifts LEFT
            bbCtx.drawImage(bb, deltaPx, 0, w - deltaPx, h, 0, 0, w - deltaPx, h);
            bbCtx.fillStyle = '#0a0a1a';
            bbCtx.fillRect(w - deltaPx, 0, deltaPx, h);
        } else {
            // Freq went DOWN → content shifts RIGHT
            const absDelta = -deltaPx;
            bbCtx.drawImage(bb, 0, 0, w - absDelta, h, absDelta, 0, w - absDelta, h);
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
            container.innerHTML = '<div class="text-center text-muted p-2" style="font-size:0.8rem;">ยังไม่มีรายการบันทึก</div>';
            return;
        }
        container.innerHTML = this.recordings.map(rec => this.createFeedItemHTML(rec)).join('');
    },

    createFeedItemHTML(rec) {
        const ts = rec.recorded_at || rec.timestamp;
        const time = ts ? new Date(ts).toLocaleTimeString('th-TH', { hour:'2-digit', minute:'2-digit' }) : '--:--';
        const freqHz = rec.frequency || (rec.frequency_mhz ? rec.frequency_mhz * 1e6 : null);
        const freq = freqHz ? (freqHz / 1e6).toFixed(3) + ' MHz' : (rec.frequency_mhz ? rec.frequency_mhz.toFixed(3) + ' MHz' : '');
        const mode = rec.mode || '';
        const dur = rec.duration ? rec.duration.toFixed(1) + 's' : '';
        const sigVal = rec.signal_strength != null ? rec.signal_strength : rec.signal_level;
        const sig = sigVal != null ? sigVal.toFixed(0) + ' dB' : '';
        const transcript = rec.transcript || rec.transcription || '<span class="text-muted">กำลังถอดเสียง...</span>';
        const audioUrl = rec.url || (rec.audio_file_name ? `/recordings/${rec.audio_file_name}` : '');
        const source = rec.source || 'auto';
        const badgeClass = source === 'manual' ? 'manual' : 'auto';
        const badgeLabel = source === 'manual' ? '✋ บันทึก' : '🔄 อัตโนมัติ';

        return `
            <div class="feed-item" id="feed-${rec.id}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span class="feed-time">${time}</span>
                    <div style="display:flex; gap:6px; align-items:center;">
                        <span class="source-badge ${badgeClass}" title="${source === 'manual' ? 'ผู้ใช้กดบันทึก' : 'บันทึกอัตโนมัติเมื่อตรวจพบสัญญาณ'}">${badgeLabel}</span>
                        <span style="font-size:0.7rem; color:var(--text-secondary);">${freq} ${mode} | ${dur} | ${sig}</span>
                    </div>
                </div>
                <div class="feed-text">
                    <div class="transcript" style="font-size:0.8rem;">${transcript}</div>
                </div>
                ${audioUrl ? `<audio controls style="width:100%;height:28px;margin-top:4px;" src="${audioUrl}"></audio>` : ''}
            </div>
        `;
    },

    addLiveFeedItem(data) {
        this.recordings.unshift(data);
        if (this.recordings.length > 20) this.recordings.pop();
        this.renderLiveFeed();
    },

    viewAllRecordings() {
        window.location.href = '/recordings';
    },

    showAddBookmark() {
        const name = prompt('ชื่อ bookmark:');
        if (!name) return;
        const freqMHz = prompt('ความถี่ (MHz):', (this.currentFreq / 1e6).toFixed(3));
        if (!freqMHz) return;
        alert(`เพิ่ม bookmark "${name}" ที่ ${freqMHz} MHz (${this.currentMode}) - กำลังพัฒนา API`);
    },
};

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});