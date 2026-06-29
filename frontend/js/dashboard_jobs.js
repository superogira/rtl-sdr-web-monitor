/**
 * Dashboard Recording Jobs UX
 *
 * Enhances the dashboard with:
 *  1. SDR device selector — picks which device is "active" for live use
 *  2. "การอัดเสียงอัตโนมัติ" panel under the clock — shows active recording
 *     jobs with freq / mode / BW and actual runtime status (recording/waiting).
 *  3. Real runtime status fetched from /api/recording-jobs/runtime-status —
 *     no longer guesses from live center frequency.
 */
(function () {
    'use strict';

    const DJ = {
        jobs: [],
        runtime: { active_captures: [], active_job_ids: [] },
        serverTz: 'Asia/Bangkok',
        activeDeviceId: null,
        autoRefreshTimer: null,

        async init() {
            this._injectStyles();
            this._injectDeviceSelector();
            this._injectJobsPanel();
            await this.refresh();
            // Refresh every 15s (catches recording start/stop + time windows)
            this._startAutoRefresh();
            // Pause the 15s poll while the dashboard tab is hidden so an idle
            // background tab stops firing 4 requests per tick at the server's
            // single event loop. Resume (with an immediate refresh) on focus.
            document.addEventListener('visibilitychange', this._onVisibility);
        },

        _startAutoRefresh() {
            if (this.autoRefreshTimer) clearInterval(this.autoRefreshTimer);
            this.autoRefreshTimer = setInterval(() => this.refresh(), 15000);
        },

        _stopAutoRefresh() {
            if (this.autoRefreshTimer) {
                clearInterval(this.autoRefreshTimer);
                this.autoRefreshTimer = null;
            }
        },

        _onVisibility() {
            // `this` is document here (listener bound without bind); use DJ.
            if (document.hidden) {
                DJ._stopAutoRefresh();
            } else {
                DJ.refresh();
                DJ._startAutoRefresh();
            }
        },

        async refresh() {
            try {
                const [jobs, tzInfo, status, runtime] = await Promise.all([
                    API.recordingJobs.list(),
                    API.recordingJobs.getServerTimezone().catch(() => ({ timezone: 'Asia/Bangkok' })),
                    API.sdr.status().catch(() => ({})),
                    API.recordingJobs.runtimeStatus().catch(() => ({})),
                ]);
                this.jobs = Array.isArray(jobs) ? jobs : [];
                this.serverTz = tzInfo.timezone || 'Asia/Bangkok';
                this.activeDeviceId = status.device_id || this.activeDeviceId;
                this.runtime = runtime || { active_captures: [], active_job_ids: [] };
                this._renderDeviceSelector();
                this._renderJobsPanel();
            } catch (e) {
                console.warn('[dashboard_jobs] refresh failed', e);
            }
        },

        // === Device selector (all logged-in users) ===
        _injectDeviceSelector() {
            const headerRight = document.querySelector('.header-right');
            if (!headerRight) return;
            if (document.getElementById('dj-device-select-wrap')) return;

            const wrap = document.createElement('div');
            wrap.id = 'dj-device-select-wrap';
            wrap.className = 'dj-device-select-wrap';
            wrap.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:0.75rem;';
            wrap.innerHTML = `
                <span style="opacity:0.7;">📡 ${I18n.t('rj.device')}</span>
                <select id="dj-device-select"
                        style="background:var(--bg-card);color:var(--text-primary);
                               border:1px solid var(--border-color);border-radius:4px;
                               padding:2px 6px;font-size:0.75rem;max-width:140px;">
                    <option value="">${I18n.t('rj.loadingDevice')}</option>
                </select>
            `;
            const settingsBtn = document.getElementById('btn-settings');
            if (settingsBtn) headerRight.insertBefore(wrap, settingsBtn);
            else headerRight.appendChild(wrap);

            const sel = document.getElementById('dj-device-select');
            sel.addEventListener('change', async (e) => {
                const id = e.target.value;
                if (!id) return;
                if (!confirm(I18n.t('rj.switchConfirm'))) {
                    this._renderDeviceSelector();
                    return;
                }
                try {
                    sel.disabled = true;
                    await API.sdr.devices.activate(id);
                    await this.refresh();
                    if (typeof App !== 'undefined' && App.syncState) await App.syncState();
                } catch (err) {
                    alert(I18n.t('rj.switchFail') + (err.detail || err.message || err));
                } finally {
                    sel.disabled = false;
                }
            });
        },

        async _renderDeviceSelector() {
            const sel = document.getElementById('dj-device-select');
            if (!sel) return;
            try {
                const devices = await API.sdr.devices.list();
                // The API may return a bare array, {devices:[...]}, or {items:[...]}.
                const list = Array.isArray(devices)
                    ? devices
                    : (devices.items || devices.devices || []);
                if (list.length === 0) {
                    sel.innerHTML = '<option value="">' + I18n.t('rj.noDevice') + '</option>';
                    return;
                }
                // Build a set of device_ids currently used by active jobs.
                const usedDeviceIds = new Set(
                    (this.runtime.active_captures || []).map(c => c.device_id)
                );
                sel.innerHTML = list.map(d => {
                    const used = usedDeviceIds.has(d.id) ? ' 🔒' + I18n.t('rj.recordingInUse') : '';
                    const active = (this.activeDeviceId && d.id === this.activeDeviceId) ? ' selected' : '';
                    return `<option value="${d.id}"${active}>${this._esc(d.name)}${used}</option>`;
                }).join('');
            } catch (e) {
                sel.innerHTML = '<option value="">' + I18n.t('rj.loadFailed') + '</option>';
            }
        },

        // === Jobs panel under the clock ===
        _injectJobsPanel() {
            const clockWidget = document.querySelector('.clock-widget');
            if (!clockWidget) return;
            if (document.getElementById('dj-jobs-panel')) return;

            const panel = document.createElement('div');
            panel.id = 'dj-jobs-panel';
            panel.className = 'dj-jobs-panel';
            panel.style.cssText = `
                margin-top:6px;padding:6px 8px;border-radius:6px;
                background:var(--bg-card);border:1px solid var(--border-color);
                font-size:0.72rem;
            `;
            panel.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;
                            font-weight:600;color:var(--text-secondary);margin-bottom:4px;">
                    <span>🔴 ${I18n.t('rj.autoRecordTitle')}</span>
                    <span id="dj-jobs-count" style="font-weight:400;opacity:0.7;"></span>
                </div>
                <div id="dj-jobs-list"></div>
            `;
            clockWidget.parentNode.insertBefore(panel, clockWidget.nextSibling);
        },

        _renderJobsPanel() {
            const listEl = document.getElementById('dj-jobs-list');
            const countEl = document.getElementById('dj-jobs-count');
            if (!listEl) return;

            const captures = this.runtime.active_captures || [];
            if (countEl) countEl.textContent = captures.length ? I18n.t('rj.tasks', captures.length) : '';

            if (captures.length === 0) {
                listEl.innerHTML = `<div style="opacity:0.55;padding:4px 0;">${I18n.t('rj.noActiveRecordings')}</div>`;
                return;
            }

            const rows = [];
            for (const cap of captures) {
                for (const f of (cap.frequencies || [])) {
                    rows.push(this._renderFreqRow(cap, f));
                }
            }

            if (rows.length === 0) {
                listEl.innerHTML = `<div style="opacity:0.55;padding:4px 0;">${I18n.t('rj.noScheduledFreqs')}</div>`;
                return;
            }
            listEl.innerHTML = rows.join('');
        },

        _renderFreqRow(capture, f) {
            const freqMHz = (f.frequency_hz / 1e6).toFixed(4);
            const bwkHz = (f.bandwidth_hz / 1e3).toFixed(0);
            const sigDb = (f.signal_level_db != null) ? f.signal_level_db.toFixed(1) : '--';
            const isAuto = !!f.auto_squelch;
            const effDb = (f.effective_squelch_db != null) ? f.effective_squelch_db.toFixed(0) : null;
            const sqlDb = (f.squelch_db != null) ? f.squelch_db.toFixed(0) : '--';
            // Auto Squelch: show "AUTO floor/eff" so the admin sees what the
            // estimator is doing. Manual: show the configured threshold.
            const sqLabel = isAuto
                ? `AUTO${effDb != null ? ' eff ' + effDb : ''}`
                  + ((f.noise_floor_db != null) ? ` (floor ${f.noise_floor_db.toFixed(0)})` : '')
                : 'sq ' + sqlDb;
            const isRec = !!f.is_recording;
            const stateText = capture.state || '';
            // Badge: show actual recording status from runtime
            let badge;
            if (stateText === 'PAUSED') {
                badge = `<span style="color:var(--accent-yellow);">⏸️ ${I18n.t('rj.paused')}</span>`;
            } else if (isRec) {
                badge = `<span class="dj-rec-dot"></span><span style="color:var(--accent-red);font-weight:600;">${I18n.t('rj.recordingInUse')}</span>`;
            } else {
                badge = `<span style="color:var(--text-secondary);opacity:0.7;">${I18n.t('rj.waitingSignal')}</span>`;
            }
            const label = f.label ? ` <span style="opacity:0.7;">(${this._esc(f.label)})</span>` : '';
            return `
                <div style="display:flex;justify-content:space-between;align-items:center;
                            padding:3px 0;border-top:1px dashed var(--border-color);">
                    <span>
                        <span style="font-family:'JetBrains Mono',monospace;font-weight:600;">
                            ${freqMHz} MHz
                        </span>${label}
                        <span style="opacity:0.7;margin-left:4px;">
                            ${this._esc(f.demod_mode)} · ${bwkHz} kHz
                        </span>
                        <span style="opacity:0.5;margin-left:4px;font-size:0.68rem;">
                            ${sigDb} dB / ${sqLabel}
                        </span>
                    </span>
                    ${badge}
                </div>
            `;
        },

        // === Helpers ===
        _esc(s) {
            if (s == null) return '';
            const map = { '&': '\x26amp;', '<': '\x26lt;', '>': '\x26gt;', '"': '\x26quot;', "'": '&#39;' };
            return String(s).replace(/[&<>"']/g, c => map[c]);
        },

        _injectStyles() {
            if (document.getElementById('dj-styles')) return;
            const st = document.createElement('style');
            st.id = 'dj-styles';
            st.textContent = `
                @keyframes dj-blink { 0%,49%{opacity:1} 50%,100%{opacity:0.35} }
                .dj-rec-dot { display:inline-block;width:6px;height:6px;border-radius:50%;
                              background:var(--accent-red);animation:dj-blink 1s infinite;
                              margin-right:4px;vertical-align:middle; }
            `;
            document.head.appendChild(st);
        },
    };

    // Expose globally
    window.DashboardJobs = DJ;

    // Auto-init when DOM is ready (after App/auth are available)
    function boot() {
        if (typeof Auth === 'undefined' || !Auth.getUser) {
            setTimeout(boot, 100);
            return;
        }
        if (!Auth.requireAuth()) return;
        DJ.init().catch(e => console.warn('[dashboard_jobs] init failed', e));
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }

    // Re-render the dynamically-injected panel text on language change.
    document.addEventListener('languagechange', () => {
        if (window.DashboardJobs) {
            window.DashboardJobs._renderJobsPanel();
            // Re-inject device selector label
            const wrap = document.getElementById('dj-device-select-wrap');
            if (wrap) {
                const span = wrap.querySelector('span');
                if (span) span.innerHTML = '📡 ' + I18n.t('rj.device');
            }
            window.DashboardJobs._renderDeviceSelector();
        }
    });
})();
