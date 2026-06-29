'use strict';
/**
 * DashboardPrefs — persistence layer for dashboard UI state.
 *
 * Design: "write-path" persistence. Every time a dashboard value changes
 * (frequency, mode, volume, VFO count, theme, etc.), the relevant setter
 * calls DashboardPrefs.set('key', value). On page load, apply() reads
 * all saved values back and calls the public setter functions to restore the
 * exact working state — surviving refresh / browser restart.
 *
 * Values are scoped per-user via a username prefix when available.
 */
(function (global) {
    const STORAGE_KEY = 'rtl_sdr_dashboard_prefs_v2';

    function userScope() {
        try {
            const auth = JSON.parse(localStorage.getItem('rtl_sdr_auth') || '{}');
            return auth.username ? String(auth.username) : '_global';
        } catch { return '_global'; }
    }

    function scopedKey() {
        return `${STORAGE_KEY}__${userScope()}`;
    }

    function loadAll() {
        try {
            const raw = localStorage.getItem(scopedKey());
            return raw ? JSON.parse(raw) : {};
        } catch { return {}; }
    }

    let _saveTimer = null;
    function saveAll(obj) {
        try {
            if (_saveTimer) clearTimeout(_saveTimer);
            _saveTimer = setTimeout(() => {
                try { localStorage.setItem(scopedKey(), JSON.stringify(obj)); }
                catch (e) { console.warn('DashboardPrefs: save failed', e); }
                _saveTimer = null;
            }, 150);
        } catch (e) { console.warn('DashboardPrefs: saveAll error', e); }
    }

    // ---- Core API ----
    const Prefs = {
        STORAGE_KEY,
        load: loadAll,
        save: saveAll,

        get(key, fallback) {
            const all = loadAll();
            const val = all[key];
            return (val === undefined || val === null) ? fallback : val;
        },

        set(key, value) {
            const all = loadAll();
            all[key] = value;
            saveAll(all);
        },

        setMany(updates) {
            const all = loadAll();
            Object.assign(all, updates);
            saveAll(all);
        },

        remove(key) {
            const all = loadAll();
            delete all[key];
            saveAll(all);
        },

        clear() { saveAll({}); },

        apply() {
            const p = loadAll();
            const app = global.App;
            const audio = global.LiveAudio;
            const keyCount = Object.keys(p).length;
            console.log(`[DashboardPrefs] apply() — key=${scopedKey()} keys=${keyCount}`, p);
            try { this._applyApp(p, app); } catch (e) { console.warn('Prefs.apply app:', e); }
            try { this._applyAudio(p, audio); } catch (e) { console.warn('Prefs.apply audio:', e); }
            return p;
        },

        _applyApp(p, app) {
            if (!app) return;
            // Theme
            if (p.theme && typeof app.setTheme === 'function') {
                try { app.setTheme(p.theme); } catch {}
            }
            // Step size
            if (p.stepSize !== undefined && app._stepSize !== undefined) {
                app._stepSize = p.stepSize;
                const btn = document.querySelector(`.step-btn[data-step="${p.stepSize}"]`);
                if (btn) document.querySelectorAll('.step-btn').forEach(b => b.classList.toggle('active', b === btn));
            }
            // Channel bandwidth
            if (p.channelBw !== undefined && app._channelBw !== undefined) {
                app._channelBw = p.channelBw;
                const btn = document.querySelector(`.bw-btn[data-bw="${p.channelBw}"]`);
                if (btn) document.querySelectorAll('.bw-btn').forEach(b => b.classList.toggle('active', b === btn));
            }
            // Mode (demodulator)
            if (p.mode && typeof app.setMode === 'function') {
                try { app.setMode(p.mode); } catch {}
            }
            // Signal tag
            if (p.signalTag !== undefined) app.signalTagEnabled = !!p.signalTag;
            // Auto-record
            if (p.autoRecord !== undefined) app._autoRecordOn = !!p.autoRecord;
            // Auto-squelch
            if (p.autoSquelch !== undefined) app._autoSquelchOn = !!p.autoSquelch;

            // --- Waterfall settings (dB range + FFT size) ---
            // These also live in their own localStorage keys for the settings
            // page; we mirror them here so the dashboard restores consistently.
            if (p.waterfallDbMin !== undefined) {
                try { localStorage.setItem('waterfall_db_min', String(p.waterfallDbMin)); } catch {}
                if (app._dbMin !== undefined) app._dbMin = p.waterfallDbMin;
            }
            if (p.waterfallDbMax !== undefined) {
                try { localStorage.setItem('waterfall_db_max', String(p.waterfallDbMax)); } catch {}
                if (app._dbMax !== undefined) app._dbMax = p.waterfallDbMax;
            }
            if (p.fftSize !== undefined) {
                try { localStorage.setItem('fft_size', String(p.fftSize)); } catch {}
            }

            // --- Gain (global) ---
            if (p.gain !== undefined) {
                const gs = document.getElementById('gain-slider');
                const gi = document.getElementById('gain-input');
                if (gs) gs.value = p.gain;
                if (gi) gi.value = p.gain;
                // Defer the API call slightly so it runs after syncState.
                setTimeout(() => { try { API.sdr.setGain(p.gain); } catch {} }, 0);
            }

            // --- Squelch (per-user/per-listener) ---
            // Remember as the user's pending SQL so it applies on next listen,
            // and push to any currently-connected audio WS.
            if (p.squelch !== undefined) {
                const ss = document.getElementById('squelch-slider');
                const si = document.getElementById('squelch-input');
                if (ss) ss.value = p.squelch;
                if (si) si.value = p.squelch;
                if (app._pendingSquelch !== undefined) app._pendingSquelch = p.squelch;
                // Apply live if listening, else it becomes the pending value.
                setTimeout(() => {
                    try {
                        if (global.LiveAudio && global.LiveAudio.anyPlaying) {
                            global.LiveAudio.setSquelch(p.squelch);
                        }
                    } catch {}
                }, 0);
            }

            // --- PPM / Frequency correction ---
            if (p.ppm !== undefined) {
                const ps = document.getElementById('ppm-slider');
                const pi = document.getElementById('ppm-input');
                if (ps) ps.value = Math.max(-100, Math.min(100, p.ppm));
                if (pi) pi.value = p.ppm;
                setTimeout(() => { try { API.sdr.setFreqCorrection(p.ppm); } catch {} }, 0);
            }

            // VFO count (1-4). Applied BEFORE per-VFO settings.
            if (p.vfoCount !== undefined) {
                const target = Math.max(1, Math.min(app._MAX_VFOS || 4, p.vfoCount | 0));
                const delta = target - (app._vfoCount || 1);
                if (delta !== 0 && typeof app.adjustVfoCount === 'function') {
                    app.adjustVfoCount(delta);
                } else {
                    app._vfoCount = target;
                }
            }
            // Per-VFO offsets + active VFO. We set offsets directly on the
            // internal _vfos array, then refresh the UI.
            if (Array.isArray(p.vfoOffsets)) {
                p.vfoOffsets.forEach((off, i) => {
                    if (app._vfos[i] && typeof off === 'number') {
                        app._setVfoOffset(i, off);
                    }
                });
            }
            // Active VFO
            if (p.activeVfo !== undefined && typeof app.selectActiveVfo === 'function') {
                try { app.selectActiveVfo(p.activeVfo); } catch {}
            }
            // Refresh VFO rows + freq display after restoring offsets/count.
            if (typeof app._updateVfoRows === 'function') app._updateVfoRows();
            if (typeof app.updateFreqDisplay === 'function') app.updateFreqDisplay();
        },

        _applyAudio(p, audio) {
            if (!audio) return;
            // Master volume
            if (p.masterVolume !== undefined && typeof audio.setVolume === 'function') {
                try { audio.setVolume(p.masterVolume); } catch {}
            }
            // Global mute
            if (p.muted !== undefined && typeof audio.setMuted === 'function') {
                try { audio.setMuted(p.muted); } catch {}
            }
            // Per-VFO mute + speaker. Also sync the DOM dropdown so the UI
            // matches the restored routing (the <select> value is independent
            // of the Web Audio graph, which only exists while listening).
            if (Array.isArray(p.perVfo)) {
                const appRef = global.App;
                const lo = (i) => appRef && appRef._vfoLower ? appRef._vfoLower(i) : String.fromCharCode(97 + i);
                p.perVfo.forEach((v, i) => {
                    if (!v) return;
                    if (v.speaker !== undefined) {
                        const sel = document.getElementById(`vfo-${lo(i)}-speaker`);
                        if (sel) sel.value = v.speaker;
                        if (typeof audio.setSpeaker === 'function') {
                            try { audio.setSpeaker(i, v.speaker); } catch {}
                        }
                    }
                    if (v.mute !== undefined && typeof audio.setMute === 'function') {
                        try { audio.setMute(i, v.mute); } catch {}
                    }
                });
            }
        },
    };

    global.DashboardPrefs = Prefs;
})(window);