/**
 * Main Application - RTL-SDR Monitor Dashboard
 */
const App = {
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
        console.log('✅ App initialized');
    },

    async syncState() {
        try {
            const status = await API.sdr.status();
            this.isConnected = status.connected || false;
            this.isStreaming = status.streaming || false;
            this.currentFreq = status.frequency || 145500000;
            this.currentMode = status.mode || 'FM';
            this.currentSignal = status.signal_level_db || -120;
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

        // Record buttons
        const btnRecord = document.getElementById('btn-record');
        if (btnRecord) btnRecord.addEventListener('click', () => this.toggleRecording());
        const btnStop = document.getElementById('btn-stop');
        if (btnStop) btnStop.addEventListener('click', () => this.stopRecording());

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

    toggleRecording() {
        this.isRecording = !this.isRecording;
        const btn = document.getElementById('btn-record');
        if (btn) btn.classList.toggle('recording', this.isRecording);
    },

    stopRecording() {
        this.isRecording = false;
        const btn = document.getElementById('btn-record');
        if (btn) btn.classList.remove('recording');
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
            // Sync sample rate (for BW display + freq-low/freq-high calculation)
            if (data.sample_rate !== undefined) {
                this._sampleRate = data.sample_rate;
                const bw = document.getElementById('radio-bw');
                if (bw) {
                    const sr = data.sample_rate;
                    bw.textContent = sr >= 1e6 ? (sr/1e6).toFixed(1)+'M' : (sr/1e3).toFixed(0)+'k';
                }
            }
            // Sync listener count (live audio)
            if (data.listeners !== undefined) {
                const el = document.getElementById('listener-count');
                if (el) {
                    el.textContent = data.listeners > 0 ? `👥 ${data.listeners}` : '';
                }
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
        });
    },

    // === Waterfall Display ===
    _waterfallBuffer: [],
    _waterfallMaxLines: 1000,
    _waterfallNeedsRedraw: true,
    _waterfallCanvasW: 0,
    _waterfallCanvasH: 0,
    _waterfallRenderRunning: false,

    initWaterfall() {
        const canvas = document.getElementById('waterfall-canvas');
        if (!canvas) return;
        this.waterfallCanvas = canvas;
        this.waterfallCtx = canvas.getContext('2d');

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
                this._waterfallNeedsRedraw = true;
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
        // Build a 256-entry RGB lookup table from current dB range
        // Called once when dB range changes → saves massive CPU in render loop
        const lut = new Uint8ClampedArray(256 * 3);
        const dbRange = this._dbMax - this._dbMin;
        for (let i = 0; i < 256; i++) {
            const norm = i / 255;
            let r, g, b;
            if (norm < 0.15) {
                const t = norm / 0.15;
                r = 0; g = 0; b = (t * 128) | 0;
            } else if (norm < 0.3) {
                const t = (norm - 0.15) / 0.15;
                r = 0; g = 0; b = (128 + t * 127) | 0;
            } else if (norm < 0.45) {
                const t = (norm - 0.3) / 0.15;
                r = 0; g = (t * 255) | 0; b = 255;
            } else if (norm < 0.6) {
                const t = (norm - 0.45) / 0.15;
                r = 0; g = 255; b = (255 * (1 - t)) | 0;
            } else if (norm < 0.75) {
                const t = (norm - 0.6) / 0.15;
                r = (t * 255) | 0; g = 255; b = 0;
            } else if (norm < 0.9) {
                const t = (norm - 0.75) / 0.15;
                r = 255; g = (255 * (1 - t)) | 0; b = 0;
            } else {
                const t = (norm - 0.9) / 0.1;
                r = 255; g = (t * 255) | 0; b = (t * 255) | 0;
            }
            lut[i*3] = r;
            lut[i*3+1] = g;
            lut[i*3+2] = b;
        }
        this._colorLUT = lut;
        this._colorLUTDbMin = this._dbMin;
        this._colorLUTDbMax = this._dbMax;
    },

    _renderWaterfall() {
        // Read live settings from localStorage
        this._dbMin = this.waterfallDbMin;
        this._dbMax = this.waterfallDbMax;
        const canvas = this.waterfallCanvas;
        const ctx = this.waterfallCtx;
        if (!canvas || !ctx) return;
        
        const w = this._waterfallCanvasW;
        const h = this._waterfallCanvasH;
        if (w <= 0 || h <= 0) return;

        const buffer = this._waterfallBuffer;
        if (buffer.length === 0) return;
        if (!this._waterfallNeedsRedraw) return;
        this._waterfallNeedsRedraw = false;

        // Rebuild color LUT if dB range changed
        if (!this._colorLUT || this._colorLUTDbMin !== this._dbMin || this._colorLUTDbMax !== this._dbMax) {
            this._buildColorLUT();
        }
        const lut = this._colorLUT;
        const dbRange = this._dbMax - this._dbMin;
        const dbMin = this._dbMin;

        const numLines = Math.min(buffer.length, h);
        const startIdx = buffer.length - numLines;

        // Use ImageData for fast bulk pixel operations
        const imgData = ctx.createImageData(w, h);
        const pixels = imgData.data;
        // Fill with background color (10, 10, 26)
        for (let i = 0; i < pixels.length; i += 4) {
            pixels[i] = 10; pixels[i+1] = 10; pixels[i+2] = 26; pixels[i+3] = 255;
        }

        // Precompute bin index mapping for each x pixel (avoids Math.floor per pixel per line)
        const numBins = buffer[startIdx] ? buffer[startIdx].length : 1024;
        const binMap = new Int32Array(w);
        for (let x = 0; x < w; x++) {
            binMap[x] = Math.floor(x / w * numBins);
        }

        // Draw each line using Color LUT (much faster: no if-else chains per pixel)
        // Flow DOWN: newest data at top (y=0), oldest at bottom
        for (let i = 0; i < numLines; i++) {
            const line = buffer[startIdx + i];
            const y = numLines - 1 - i;
            const rowOffset = y * w * 4;

            for (let x = 0; x < w; x++) {
                const val = line[binMap[x]];
                // Map dB to LUT index (0-255)
                let lutIdx = ((val - dbMin) / dbRange * 255) | 0;
                if (lutIdx < 0) lutIdx = 0;
                else if (lutIdx > 255) lutIdx = 255;
                const lutOff = lutIdx * 3;
                const off = rowOffset + x * 4;
                pixels[off] = lut[lutOff];
                pixels[off+1] = lut[lutOff+1];
                pixels[off+2] = lut[lutOff+2];
            }
        }

        ctx.putImageData(imgData, 0, 0);

        // Draw frequency grid overlay (vertical lines + top labels)
        this._drawFreqGrid(ctx, w, h);
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

            // Vertical grid line
            if (isCenter) {
                ctx.strokeStyle = "rgba(234, 179, 8, 0.7)";
                ctx.lineWidth = 1.5;
            } else {
                ctx.strokeStyle = "rgba(255, 255, 255, 0.18)";
                ctx.lineWidth = 1;
            }
            ctx.beginPath();
            ctx.moveTo(Math.round(x) + 0.5, 0);
            ctx.lineTo(Math.round(x) + 0.5, h);
            ctx.stroke();

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
        
        const speed = this.waterfallSpeed;
        const line = new Float32Array(spectrumData);
        for (let i = 0; i < speed; i++) {
            this._waterfallBuffer.push(line);
        }
        while (this._waterfallBuffer.length > this._waterfallMaxLines) {
            this._waterfallBuffer.shift();
        }
        this._waterfallNeedsRedraw = true;
    },

    spectrumToColor(db) {
        // Not used anymore - colors computed inline in _renderWaterfall
        return '#000';
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
        const time = rec.timestamp ? new Date(rec.timestamp).toLocaleTimeString('th-TH', { hour:'2-digit', minute:'2-digit' }) : '--:--';
        const freq = rec.frequency ? (rec.frequency / 1e6).toFixed(3) + ' MHz' : '';
        const mode = rec.mode || '';
        const dur = rec.duration ? rec.duration.toFixed(1) + 's' : '';
        const sig = rec.signal_level ? rec.signal_level.toFixed(0) + ' dB' : '';
        const transcript = rec.transcription || '<span class="text-muted">กำลังถอดเสียง...</span>';
        const audioUrl = rec.url || '';

        return `
            <div class="feed-item" id="feed-${rec.id}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span class="feed-time">${time}</span>
                    <span style="font-size:0.7rem; color:var(--text-secondary);">${freq} ${mode} | ${dur} | ${sig}</span>
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