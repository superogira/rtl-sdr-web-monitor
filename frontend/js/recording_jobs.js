/**
 * RecordingJobsUI — UI logic for the "การตั้งค่าการอัดเสียงของอุปกรณ์" section
 * in settings.html (recording tab).
 *
 * Handles: list jobs, open editor, add/remove windows + frequencies, validation
 * of passband membership, device exclusivity, enable/disable, delete, and the
 * server-timezone setting.
 */
const RecordingJobsUI = {
    _jobs: [],
    _devices: [],
    _usedDeviceIds: [],
    _editingJobId: null,
    _serverTimezone: 'Asia/Bangkok',

    DAY_LABELS: ['จ', 'อ', 'พ', 'พฤ', 'ศ', 'ส', 'อา'], // 0=Mon..6=Sun (Thai short)
    DEMOD_MODES: ['FM', 'AM', 'USB', 'LSB'],
    BW_PRESETS: [
        { v: 6000, label: '6 kHz' },
        { v: 12000, label: '12 kHz' },
        { v: 25000, label: '25 kHz' },
        { v: 50000, label: '50 kHz' },
        { v: 200000, label: '200 kHz' },
    ],

    async init() {
        await this.loadServerTimezone();
        await this.refresh();
    },

    // ---- helpers ----
    _hzToMhz(hz) {
        // Always 4 decimals in MHz (= 100 Hz resolution) so values round-trip
        // correctly through the editor regardless of frequency band.
        return ((Number(hz) || 0) / 1e6).toFixed(4);
    },

    _mhzToHz(mhz) {
        const v = parseFloat(mhz);
        return Number.isFinite(v) ? Math.round(v * 1e6) : 0;
    },

    // Get the recording defaults (gain / freq_correction_ppm) stored on a
    // device's config. Falls back to 30 / 0 when not set (e.g. legacy devices).
    _getDeviceDefaults(deviceId) {
        const d = this._devices.find(x => x.id === deviceId);
        if (!d || !d.config) return { gain: 30, freq_correction_ppm: 0 };
        const cfg = d.config;
        const g = (cfg.gain !== undefined && cfg.gain !== null) ? parseInt(cfg.gain) : 30;
        const p = (cfg.freq_correction_ppm !== undefined && cfg.freq_correction_ppm !== null)
            ? parseInt(cfg.freq_correction_ppm) : 0;
        return {
            gain: Number.isFinite(g) ? g : 30,
            freq_correction_ppm: Number.isFinite(p) ? p : 0,
        };
    },

    // Apply a device's recording defaults to all windows in the editor.
    // Used when a new job is created or when the user changes the device — so
    // the admin doesn't have to retype Gain/PPM every time (but can still edit).
    //
    // IMPORTANT: We update the internal model AND the DOM inputs directly.
    // We must NOT call _renderWindows() here because it calls _syncFromDOM()
    // first, which would read the OLD values from the DOM and overwrite the
    // defaults we just set.
    applyDeviceDefaultsToWindows(deviceId) {
        const def = this._getDeviceDefaults(deviceId);
        (this._editorWindows || []).forEach(w => {
            w.gain = def.gain;
            w.freq_correction_ppm = def.freq_correction_ppm;
            // Update the DOM inputs directly (they already exist after first render)
            const gEl = document.getElementById(`w-${w._tempId}-gain`);
            const pEl = document.getElementById(`w-${w._tempId}-ppm`);
            if (gEl) gEl.value = def.gain;
            if (pEl) pEl.value = def.freq_correction_ppm;
        });
    },

    // Called when the device <select> changes in the editor.
    onDeviceChange() {
        const sel = document.getElementById('rj-device');
        const deviceId = sel ? sel.value : '';
        if (deviceId) this.applyDeviceDefaultsToWindows(deviceId);
    },

    _escape(s) {
        // Uses \x escapes in source so entities survive auto-formatting.
        const A = String.fromCharCode(38);  // &
        return String(s == null ? '' : s)
            .replace(/\x26/g, A + 'amp;')
            .replace(/\x3c/g, A + 'lt;')
            .replace(/\x3e/g, A + 'gt;')
            .replace(/\x22/g, A + 'quot;')
            .replace(/\x27/g, A + '#39;');
    },

    async loadServerTimezone() {
        try {
            const res = await API.recordingJobs.getServerTimezone();
            this._serverTimezone = res.timezone || 'Asia/Bangkok';
        } catch (e) {
            console.warn('loadServerTimezone failed', e);
        }
        const el = document.getElementById('server-timezone');
        if (el) el.value = this._serverTimezone;
    },

    async saveServerTimezone() {
        const el = document.getElementById('server-timezone');
        const tz = (el && el.value || '').trim();
        if (!tz) { alert('กรุณาใส่เขตเวลา'); return; }
        try {
            await API.recordingJobs.setServerTimezone(tz);
            this._serverTimezone = tz;
            alert('✅ บันทึกเขตเวลาเรียบร้อย');
        } catch (e) {
            alert('บันทึกไม่สำเร็จ: ' + (e.detail || e.message || e));
        }
    },

    async refresh() {
        // Fetch jobs + available devices in parallel
        const [jobsRes, devRes] = await Promise.all([
            API.recordingJobs.list().catch(() => []),
            API.recordingJobs.availableDevices().catch(() => ({ devices: [], used_device_ids: [] })),
        ]);
        this._jobs = Array.isArray(jobsRes) ? jobsRes : [];
        this._devices = (devRes && devRes.devices) || [];
        this._usedDeviceIds = (devRes && devRes.used_device_ids) || [];
        this._renderList();
    },

    // ---- list rendering ----
    _renderList() {
        const el = document.getElementById('recording-jobs-list');
        if (!el) return;
        if (!this._jobs.length) {
            el.innerHTML = '<div class="text-center text-muted" style="padding:16px;">ยังไม่มีการตั้งค่าการอัดเสียงของอุปกรณ์</div>';
            return;
        }
        el.innerHTML = this._jobs.map(j => this._renderJobCard(j)).join('');
    },

    _deviceName(id) {
        const d = this._devices.find(x => x.id === id);
        return d ? d.name : '(ไม่พบอุปกรณ์)';
    },

    _renderJobCard(j) {
        const devName = this._escape(this._deviceName(j.device_id));
        const enabledBadge = j.enabled
            ? '<span class="status-badge status-on">เปิดใช้งาน</span>'
            : '<span class="status-badge status-off">ปิดอยู่</span>';
        const modeLabel = j.mode === 'scheduled' ? 'ตามช่วงเวลา' : 'ตลอดเวลา';

        // Summarize windows
        const winSummary = (j.windows || []).map(w => {
            const lo = w.start_freq_hz;
            const hi = lo + w.sample_rate;
            const timeStr = (j.mode === 'scheduled')
                ? `${w.start_time || '?'}–${w.end_time || '?'}`
                : 'ตลอดเวลา';
            const days = (j.mode === 'scheduled' && w.days && w.days.length)
                ? w.days.map(d => this.DAY_LABELS[d]).join(',')
                : 'ทุกวัน';
            const freqs = (w.frequencies || []).map(f =>
                `${this._hzToMhz(f.frequency_hz)} MHz (${f.demod_mode}, BW ${(f.bandwidth_hz/1000)}kHz, ${f.auto_squelch ? 'SQ AUTO' : 'SQ ' + f.squelch_db + 'dB'})`
            ).join('; ');
            return `<div style="font-size:0.74rem; color:var(--text-secondary); margin-top:4px;">
                <b>${timeStr}</b> [${days}] · ช่วง ${this._hzToMhz(lo)}–${this._hzToMhz(hi)} MHz · center ${this._hzToMhz(w.center_freq_hz)} MHz<br>
                📻 ${freqs || '(ไม่มีความถี่)'}
            </div>`;
        }).join('');

        return `
        <div style="background:var(--surface-1); border:1px solid var(--border); border-radius:6px; padding:12px; margin-bottom:8px;">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
                <div style="flex:1; min-width:200px;">
                    <strong>${this._escape(j.name || devName)}</strong>
                    ${enabledBadge}
                    <span class="status-badge" style="background:rgba(14,165,233,0.15); color:var(--accent-cyan);">${modeLabel}</span>
                    <div style="font-size:0.78rem; color:var(--text-secondary);">🔌 ${devName}</div>
                </div>
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button class="btn btn-sm" onclick="RecordingJobsUI.toggleEnabled('${j.id}', ${!j.enabled})">
                        ${j.enabled ? '⏸️ ปิดใช้งาน' : '▶️ เปิดใช้งาน'}
                    </button>
                    <button class="btn btn-sm" onclick="RecordingJobsUI.openEditor('${j.id}')">✏️ แก้ไข</button>
                    <button class="btn btn-sm" style="color:var(--accent-red);" onclick="RecordingJobsUI.remove('${j.id}')">🗑️ ลบ</button>
                </div>
            </div>
            ${winSummary}
        </div>`;
    },

    async toggleEnabled(id, enabled) {
        try {
            await API.recordingJobs.setEnabled(id, enabled);
            await this.refresh();
        } catch (e) {
            alert('ไม่สามารถเปลี่ยนสถานะ: ' + (e.detail || e.message || e));
        }
    },

    async remove(id) {
        if (!confirm('ต้องการลบการตั้งค่าการอัดเสียงนี้?')) return;
        try {
            await API.recordingJobs.remove(id);
            await this.refresh();
        } catch (e) {
            alert('ลบไม่สำเร็จ: ' + (e.detail || e.message || e));
        }
    },

    // ---- editor ----
    openEditor(jobId) {
        this._editingJobId = jobId || null;
        const job = jobId ? this._jobs.find(j => j.id === jobId) : null;
        this._showEditorModal(job);
    },

    _availableDeviceOptions(selectedId, currentDeviceId) {
        // A device is selectable if it's free OR it's the current job's device.
        const opts = [];
        for (const d of this._devices) {
            const used = this._usedDeviceIds.includes(d.id);
            const isCurrent = currentDeviceId && d.id === currentDeviceId;
            if (used && !isCurrent) continue;
            const sel = (selectedId === d.id) ? 'selected' : '';
            opts.push(`<option value="${d.id}" ${sel}>${this._escape(d.name)}</option>`);
        }
        if (!opts.length) {
            return '<option value="">— ไม่มีอุปกรณ์ที่ว่างให้เลือกใช้งาน —</option>';
        }
        return '<option value="">— เลือกอุปกรณ์ —</option>' + opts.join('');
    },

    _showEditorModal(job) {
        const editing = !!job;
        const mode = job ? job.mode : 'always';
        const name = job ? (job.name || '') : '';
        // For a NEW job, auto-select the first free device so its recording
        // defaults (Gain / PPM) can pre-fill the windows.
        let deviceId = job ? job.device_id : '';
        if (!editing) {
            const firstFree = this._devices.find(d => !this._usedDeviceIds.includes(d.id));
            if (firstFree) deviceId = firstFree.id;
        }
        const windows = job ? (job.windows || []) : [this._blankWindow('always')];

        // Build modal container
        let modal = document.getElementById('rj-editor-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'rj-editor-modal';
            modal.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.75); backdrop-filter:blur(4px); -webkit-backdrop-filter:blur(4px); z-index:9999; display:flex; align-items:flex-start; justify-content:center; padding:24px; overflow:auto;';
            document.body.appendChild(modal);
        }

        modal.innerHTML = `
        <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:8px; width:100%; max-width:760px; padding:16px; box-shadow:0 20px 60px rgba(0,0,0,0.6);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <h2 style="margin:0; font-size:1.1rem; color:var(--accent-cyan);">${editing ? '✏️ แก้ไขการอัดเสียง' : '➕ เพิ่มการอัดเสียง'}</h2>
                <button class="btn btn-sm" onclick="RecordingJobsUI.closeEditor()">✖ ปิด</button>
            </div>

            <div class="form-group">
                <label>ชื่อการอัดเสียง (ไม่บังคับ)</label>
                <input type="text" id="rj-name" value="${this._escape(name)}" placeholder="เช่น อัด VHF ช่วงเช้า">
            </div>

            <div class="form-row">
                <div class="form-group">
                    <label>อุปกรณ์ SDR</label>
                    <select id="rj-device" onchange="RecordingJobsUI.onDeviceChange()">${this._availableDeviceOptions(deviceId, deviceId)}</select>
                    <p id="rj-device-defaults-hint" style="font-size:0.7rem; color:var(--text-secondary); margin-top:4px;"></p>
                </div>
                <div class="form-group">
                    <label>โหมดการอัด</label>
                    <select id="rj-mode" onchange="RecordingJobsUI.onModeChange()">
                        <option value="always" ${mode==='always'?'selected':''}>ตลอดเวลา</option>
                        <option value="scheduled" ${mode==='scheduled'?'selected':''}>ตามช่วงเวลา</option>
                    </select>
                </div>
            </div>

            <div id="rj-windows"></div>

            <div class="form-actions">
                <button class="btn btn-sm" onclick="RecordingJobsUI.addWindow()">➕ เพิ่มช่วงเวลา</button>
                <button class="btn btn-primary" onclick="RecordingJobsUI.save()">💾 บันทึก</button>
            </div>
        </div>`;

        modal.style.display = 'flex';

        // Store window data internally and render
        this._editorWindows = windows.map(w => this._cloneWindow(w));
        this._editorMode = mode;
        // For a NEW job, pre-fill the blank window's Gain/PPM from the device's
        // recording defaults. (When editing, keep the job's existing values.)
        if (!editing && deviceId) {
            const def = this._getDeviceDefaults(deviceId);
            this._editorWindows.forEach(w => {
                w.gain = def.gain;
                w.freq_correction_ppm = def.freq_correction_ppm;
            });
        }
        this._updateDeviceDefaultsHint(deviceId);
        this._renderWindows();
    },

    // Show a small hint showing the selected device's recording defaults so the
    // admin understands where the auto-filled Gain/PPM values come from.
    _updateDeviceDefaultsHint(deviceId) {
        const el = document.getElementById('rj-device-defaults-hint');
        if (!el) return;
        if (!deviceId) { el.textContent = ''; return; }
        const def = this._getDeviceDefaults(deviceId);
        el.textContent = `ค่าเริ่มต้นจากอุปกรณ์: Gain ${def.gain} dB · PPM ${def.freq_correction_ppm} (ใช้กรอกอัตโนมัติด้านล่าง แก้ไขเองได้)`;
    },

    closeEditor() {
        const modal = document.getElementById('rj-editor-modal');
        if (modal) modal.style.display = 'none';
        this._editingJobId = null;
        this._editorWindows = [];
    },

    _blankWindow(mode) {
        return {
            _tempId: 'w' + Math.random().toString(36).slice(2, 8),
            start_time: '08:00',
            end_time: '10:00',
            days: [0, 1, 2, 3, 4], // default Mon-Fri
            gain: 30,
            freq_correction_ppm: 0,
            start_freq_hz: 144000000,
            sample_rate: 2048000,
            frequencies: [this._blankFreq(144825000)],
        };
    },

    _blankFreq(freq) {
        return {
            _tempId: 'f' + Math.random().toString(36).slice(2, 8),
            frequency_hz: freq || 0,
            squelch_db: -70,
            auto_squelch: false,
            demod_mode: 'FM',
            bandwidth_hz: 12000,
            label: '',
        };
    },

    _cloneWindow(w) {
        return JSON.parse(JSON.stringify({
            ...w,
            _tempId: w._tempId || 'w' + Math.random().toString(36).slice(2, 8),
            days: w.days || [],
            frequencies: (w.frequencies || []).map(f => ({
                ...f,
                _tempId: f._tempId || 'f' + Math.random().toString(36).slice(2, 8),
            })),
        }));
    },

    onAutoSquelchChange(freqTempId) {
        const cb = document.getElementById(`f-${freqTempId}-autosq`);
        const sq = document.getElementById(`f-${freqTempId}-sq`);
        if (!cb || !sq) return;
        sq.disabled = cb.checked;
        if (cb.checked) {
            sq.style.opacity = '0.5';
        } else {
            sq.style.opacity = '';
        }
    },

    onModeChange() {
        const sel = document.getElementById('rj-mode');
        this._editorMode = sel ? sel.value : 'always';
        // If switching to "always", collapse to single window
        if (this._editorMode === 'always') {
            if (this._editorWindows.length !== 1) {
                this._editorWindows = [this._editorWindows[0] || this._blankWindow('always')];
            }
        } else {
            // Ensure at least one scheduled window exists
            if (!this._editorWindows.length) {
                this._editorWindows = [this._blankWindow('scheduled')];
            }
        }
        this._renderWindows();
    },

    addWindow() {
        this._editorWindows.push(this._blankWindow(this._editorMode));
        this._renderWindows();
    },

    removeWindow(tempId) {
        this._editorWindows = this._editorWindows.filter(w => w._tempId !== tempId);
        if (!this._editorWindows.length) {
            this._editorWindows = [this._blankWindow(this._editorMode)];
        }
        this._renderWindows();
    },

    addFreq(winTempId) {
        const w = this._editorWindows.find(x => x._tempId === winTempId);
        if (!w) return;
        // default new freq to the center of the passband
        const center = Math.round((w.start_freq_hz || 0) + (w.sample_rate || 0) / 2);
        w.frequencies.push(this._blankFreq(center));
        this._renderWindows();
    },

    removeFreq(winTempId, freqTempId) {
        const w = this._editorWindows.find(x => x._tempId === winTempId);
        if (!w) return;
        w.frequencies = w.frequencies.filter(f => f._tempId !== freqTempId);
        this._renderWindows();
    },

    // Read current DOM values back into _editorWindows before re-rendering.
    // IMPORTANT: On first render the DOM inputs do not exist yet, so we must
    // bail out — otherwise getElementById returns null and parseInt(null)||0
    // overwrites the real (loaded) values with zeros.
    _syncFromDOM() {
        const mode = this._editorMode;
        this._editorWindows.forEach(w => {
            const p = (id) => document.getElementById(id);
            // Guard: if the primary input for this window isn't in the DOM yet,
            // skip syncing it (first render / partial render) to preserve data.
            if (!p(`w-${w._tempId}-gain`)) return;
            const gp = (id) => { const el = p(id); return el ? el.value : null; };
            w.gain = parseInt(gp(`w-${w._tempId}-gain`)) || 0;
            w.freq_correction_ppm = parseInt(gp(`w-${w._tempId}-ppm`)) || 0;
            w.start_freq_hz = this._mhzToHz(gp(`w-${w._tempId}-start`));
            w.sample_rate = parseInt(gp(`w-${w._tempId}-sr`)) || 0;
            if (mode === 'scheduled') {
                w.start_time = gp(`w-${w._tempId}-st`) || '';
                w.end_time = gp(`w-${w._tempId}-et`) || '';
                // days checkboxes
                const days = [];
                for (let i = 0; i < 7; i++) {
                    const cb = p(`w-${w._tempId}-day-${i}`);
                    if (cb && cb.checked) days.push(i);
                }
                w.days = days;
            }
            w.frequencies.forEach(f => {
                f.frequency_hz = this._mhzToHz(gp(`f-${f._tempId}-freq`));
                f.squelch_db = parseFloat(gp(`f-${f._tempId}-sq`));
                f.auto_squelch = !!(document.getElementById(`f-${f._tempId}-autosq`) || {}).checked;
                f.demod_mode = gp(`f-${f._tempId}-demod`) || 'FM';
                f.bandwidth_hz = parseInt(gp(`f-${f._tempId}-bw`)) || 12000;
                f.label = gp(`f-${f._tempId}-label`) || '';
            });
        });
    },

    _renderWindows() {
        // Sync current values before re-rendering (preserves user input)
        this._syncFromDOM();
        const container = document.getElementById('rj-windows');
        if (!container) return;
        const mode = this._editorMode;
        const addWindowBtn = (mode === 'scheduled')
            ? `<button class="btn btn-sm" onclick="RecordingJobsUI.addWindow()">➕ เพิ่มช่วงเวลา</button>`
            : '';
        container.innerHTML = this._editorWindows.map((w, idx) => this._renderWindow(w, idx, mode)).join('') + addWindowBtn;
    },

    _renderWindow(w, idx, mode) {
        const lo = w.start_freq_hz;
        const hi = lo + w.sample_rate;
        const center = lo + Math.floor(w.sample_rate / 2);

        const timeBlock = (mode === 'scheduled') ? `
            <div class="form-row">
                <div class="form-group">
                    <label>เวลาเริ่ม (HH:MM)</label>
                    <input type="time" id="w-${w._tempId}-st" value="${this._escape(w.start_time || '08:00')}">
                </div>
                <div class="form-group">
                    <label>เวลาสิ้นสุด (HH:MM)</label>
                    <input type="time" id="w-${w._tempId}-et" value="${this._escape(w.end_time || '10:00')}">
                </div>
            </div>
            <div class="form-group">
                <label>วันในสัปดาห์</label>
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    ${[0,1,2,3,4,5,6].map(d => {
                        const checked = (w.days || []).includes(d) ? 'checked' : '';
                        return `<label style="font-size:0.8rem; display:flex; align-items:center; gap:2px;">
                            <input type="checkbox" id="w-${w._tempId}-day-${d}" ${checked}> ${this.DAY_LABELS[d]}
                        </label>`;
                    }).join('')}
                </div>
            </div>
        ` : '';

        const removeWinBtn = (mode === 'scheduled')
            ? `<button class="btn btn-sm" style="color:var(--accent-red); margin-left:8px;" onclick="RecordingJobsUI.removeWindow('${w._tempId}')">🗑️ ลบช่วงเวลา</button>`
            : '';

        const freqsHtml = w.frequencies.map(f => this._renderFreq(f, w._tempId, lo, hi)).join('');
        const freqsBlock = `
            <div style="margin-top:8px;">
                <label style="font-size:0.8rem; color:var(--text-secondary);">ความถี่ที่จะอัด (ต้องอยู่ในช่วง ${this._hzToMhz(lo)}–${this._hzToMhz(hi)} MHz)</label>
                ${freqsHtml}
                <button class="btn btn-sm" style="margin-top:6px;" onclick="RecordingJobsUI.addFreq('${w._tempId}')">➕ เพิ่มความถี่</button>
            </div>`;

        return `
        <div style="border:1px dashed var(--border); border-radius:6px; padding:10px; margin-bottom:10px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <strong style="font-size:0.85rem;">ช่วงเวลา #${idx + 1}</strong>
                ${removeWinBtn}
            </div>
            ${timeBlock}
            <div class="form-row">
                <div class="form-group">
                    <label>Gain (dB)</label>
                    <input type="number" id="w-${w._tempId}-gain" value="${w.gain}" min="0" max="60">
                </div>
                <div class="form-group">
                    <label>Frequency Correction (PPM)</label>
                    <input type="number" id="w-${w._tempId}-ppm" value="${w.freq_correction_ppm}" min="-200" max="200">
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>ความถี่เริ่มต้น (MHz)</label>
                    <input type="number" id="w-${w._tempId}-start" value="${this._hzToMhz(w.start_freq_hz)}" step="0.0001" onchange="RecordingJobsUI._renderWindows()">
                    <div style="font-size:0.72rem; color:var(--text-secondary);">${w.start_freq_hz.toLocaleString()} Hz</div>
                </div>
                <div class="form-group">
                    <label>Sample Rate (Hz)</label>
                    <input type="number" id="w-${w._tempId}-sr" value="${w.sample_rate}" onchange="RecordingJobsUI._renderWindows()">
                    <div style="font-size:0.72rem; color:var(--text-secondary);">ช่วง ${this._hzToMhz(lo)}–${this._hzToMhz(hi)} MHz · center ${this._hzToMhz(center)} MHz</div>
                </div>
            </div>
            ${freqsBlock}
        </div>`;
    },

    _renderFreq(f, winTempId, lo, hi) {
        const inBand = (f.frequency_hz >= lo && f.frequency_hz <= hi);
        const warn = inBand ? '' : `<div style="font-size:0.72rem; color:var(--accent-red);">⚠️ เกินช่วงความถี่ — จะบันทึกไม่ได้</div>`;
        return `
        <div style="background:var(--surface-1); border:1px solid var(--border); border-radius:4px; padding:8px; margin-top:6px;">
            <div class="form-row" style="grid-template-columns: 1fr 1fr 1fr 1fr;">
                <div class="form-group" style="margin-bottom:4px;">
                    <label style="font-size:0.72rem;">ความถี่ (MHz)</label>
                    <input type="number" id="f-${f._tempId}-freq" value="${this._hzToMhz(f.frequency_hz)}" step="0.0001" onchange="RecordingJobsUI._renderWindows()" style="font-size:0.78rem;">
                </div>
                <div class="form-group" style="margin-bottom:4px;">
                    <label style="font-size:0.72rem;">Squelch (dB)</label>
                    <input type="number" id="f-${f._tempId}-sq" value="${f.squelch_db}" step="1" style="font-size:0.78rem;" ${f.auto_squelch ? 'disabled' : ''}>
                    <label style="font-size:0.72rem; display:flex; align-items:center; gap:4px; margin-top:4px; cursor:pointer;">
                        <input type="checkbox" id="f-${f._tempId}-autosq" ${f.auto_squelch ? 'checked' : ''} onchange="RecordingJobsUI.onAutoSquelchChange('${f._tempId}')" style="margin:0;">
                        Auto Squelch
                    </label>
                </div>
                <div class="form-group" style="margin-bottom:4px;">
                    <label style="font-size:0.72rem;">Demod</label>
                    <select id="f-${f._tempId}-demod" style="font-size:0.78rem;">
                        ${this.DEMOD_MODES.map(m => `<option value="${m}" ${f.demod_mode===m?'selected':''}>${m}</option>`).join('')}
                    </select>
                </div>
                <div class="form-group" style="margin-bottom:4px;">
                    <label style="font-size:0.72rem;">Bandwidth (Hz)</label>
                    <select id="f-${f._tempId}-bw" style="font-size:0.78rem;">
                        ${this.BW_PRESETS.map(b => `<option value="${b.v}" ${f.bandwidth_hz===b.v?'selected':''}>${b.label}</option>`).join('')}
                    </select>
                </div>
            </div>
            <div class="form-row" style="grid-template-columns: 1fr auto;">
                <div class="form-group" style="margin-bottom:0;">
                    <label style="font-size:0.72rem;">หมายเหตุ (ไม่บังคับ)</label>
                    <input type="text" id="f-${f._tempId}-label" value="${this._escape(f.label || '')}" placeholder="เช่น 144.825" style="font-size:0.78rem;">
                </div>
                <div class="form-group" style="margin-bottom:0; display:flex; align-items:flex-end;">
                    <button class="btn btn-sm" style="color:var(--accent-red);" onclick="RecordingJobsUI.removeFreq('${winTempId}','${f._tempId}')">ลบ</button>
                </div>
            </div>
            ${warn}
        </div>`;
    },

    // ---- save ----
    async save() {
        this._syncFromDOM();
        const name = (document.getElementById('rj-name').value || '').trim();
        const deviceId = document.getElementById('rj-device').value;
        const mode = this._editorMode;
        if (!deviceId) {
            alert('กรุณาเลือกอุปกรณ์ SDR (หรือไม่มีอุปกรณ์ที่ว่าง)');
            return;
        }
        const payload = {
            name: name || null,
            device_id: deviceId,
            mode,
            enabled: true,
            windows: this._editorWindows.map(w => ({
                start_time: mode === 'scheduled' ? (w.start_time || '') : null,
                end_time: mode === 'scheduled' ? (w.end_time || '') : null,
                days: mode === 'scheduled' ? (w.days || []) : null,
                gain: w.gain,
                freq_correction_ppm: w.freq_correction_ppm,
                start_freq_hz: w.start_freq_hz,
                sample_rate: w.sample_rate,
                frequencies: (w.frequencies || []).map(f => ({
                    frequency_hz: f.frequency_hz,
                    squelch_db: f.squelch_db,
                    auto_squelch: !!f.auto_squelch,
                    demod_mode: f.demod_mode,
                    bandwidth_hz: f.bandwidth_hz,
                    label: f.label || null,
                })),
            })),
        };
        try {
            if (this._editingJobId) {
                await API.recordingJobs.update(this._editingJobId, payload);
            } else {
                await API.recordingJobs.create(payload);
            }
            this.closeEditor();
            await this.refresh();
        } catch (e) {
            alert('บันทึกไม่สำเร็จ: ' + (e.detail || e.message || e));
        }
    },
};

// Expose globally for inline onclick handlers
window.RecordingJobsUI = RecordingJobsUI;