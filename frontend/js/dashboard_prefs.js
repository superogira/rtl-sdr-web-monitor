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
    // VERSION — bump on every code change so the console log confirms the
    // browser loaded the latest file (catches stale-cache issues).
    const VERSION = '2026.06.29-v2';

    const STORAGE_KEY = 'rtl_sdr_dashboard_prefs_v2';

    function userScope() {
        // auth.js stores the logged-in user under localStorage key 'user'
        // (NOT 'rtl_sdr_auth'). Read it here so per-user prefs are scoped
        // correctly and don't collide between users sharing one browser.
        try {
            const user = JSON.parse(localStorage.getItem('user') || 'null');
            if (user && user.username) return String(user.username);
            return '_global';
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
        VERSION,
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
            // app.js / audio.js declare 'const App' / 'const LiveAudio' at the
            // top level of their <script>. These create SCRIPT-scope globals
            // that are NOT properties of window (unlike 'var' would be), so
            // 'global.App' is undefined here. Resolve via the bare identifier
            // first, falling back to global.* for safety.
            const app = (typeof App !== 'undefined') ? App : global.App;
            const audio = (typeof LiveAudio !== 'undefined') ? LiveAudio : global.LiveAudio;
            const keyCount = Object.keys(p).length;
            console.log(
                `%c[DashboardPrefs v${VERSION}] apply()`,
                'color:#4af;font-weight:bold',
                `\n  scope=${userScope()}\n  key=${scopedKey()}\n  keys=${keyCount}`,
                p
            );
            if (keyCount === 0) {
                console.warn(`[DashboardPrefs v${VERSION}] No saved prefs found — nothing to restore. (First visit on this browser/user, or localStorage was cleared.)`);
            }
            try { this._applyApp(p, app); } catch (e) { console.warn('Prefs.apply app:', e); }
            try { this._applyAudio(p, audio); } catch (e) { console.warn('Prefs.apply audio:', e); }
            return p;
        },

        _applyApp(p, app) {
            if (!app) {
                console.warn(`[DashboardPrefs v${VERSION}] App not available — skipping app prefs`);
                return;
            }
            // Theme
            if (p.theme && typeof app.setTheme === 'function') {
                console.log(`[DashboardPrefs] restore theme=${p.theme}`);
                try { app.setTheme(p.theme); } catch {}
            }
            // Step size
            if (p.stepSize !== undefined && app._stepSize !== undefined) {
                console.log(`[DashboardPrefs] restore stepSize=${p.stepSize}`);
                app._stepSize = p.stepSize;
                const btn = document.querySelector(`.step-btn[data-step="${p.stepSize}"]`);
                if (btn) document.querySelectorAll('.step-btn').forEach(b => b.classList.toggle('active', b === btn));
            }
            // Channel bandwidth
            if (p.channelBw !== undefined && app._channelBw !== undefined) {
                console.log(`[DashboardPrefs] restore channelBw=${p.channelBw}`);
                // Use App's setter so the buttons, the trigger label AND the
                // live listener / global BW all get the restored value. The old
                // code only changed `app._channelBw` + the button class, so a
                // refresh showed 25k while the DSP still demodulated 12k.
                if (typeof app._setChannelBw === 'function') {
                    app._setChannelBw(p.channelBw);
                } else {
                    app._channelBw = p.channelBw;
                    const btn = document.querySelector(`.bw-btn[data-bw="${p.channelBw}"]`);
                    if (btn) document.querySelectorAll('.bw-btn').forEach(b => b.classList.toggle('active', b === btn));
                }
            }
            // Mode (demodulator)
            if (p.mode && typeof app.setMode === 'function') {
                console.log(`[DashboardPrefs] restore mode=${p.mode}`);
                try { app.setMode(p.mode); } catch {}
            }
            // Signal tag
            if (p.signalTag !== undefined) {
                console.log(`[DashboardPrefs] restore signalTag=${p.signalTag}`);
                app.signalTagEnabled = !!p.signalTag;
            }
            // Auto-record
            if (p.autoRecord !== undefined) {
                console.log(`[DashboardPrefs] restore autoRecord=${p.autoRecord}`);
                app._autoRecordOn = !!p.autoRecord;
            }
            // Auto-squelch
            if (p.autoSquelch !== undefined) {
                console.log(`[DashboardPrefs] restore autoSquelch=${p.autoSquelch}`);
                app._autoSquelchOn = !!p.autoSquelch;
            }

            // --- Waterfall settings (dB range + FFT size) ---
            // FFT size is mirrored here because the settings page writes the
            // same localStorage key.
            //
            // NOTE: the dB min/max range is deliberately NOT restored from the
            // prefs blob. Nothing writes `waterfallDbMin/Max` into it any more,
            // so it could only ever restore STALE values on top of the ones the
            // user saved in Settings → Display (or the ones ONCE computed),
            // which made the Display tab look like it ignored every change.
            if (p.fftSize !== undefined) {
                try { localStorage.setItem('fft_size', String(p.fftSize)); } catch {}
            }

            // --- Gain (global) ---
            if (p.gain !== undefined) {
                console.log(`[DashboardPrefs] restore gain=${p.gain}`);
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
                console.log(`[DashboardPrefs] restore squelch=${p.squelch}`);
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
                console.log(`[DashboardPrefs] restore ppm=${p.ppm}`);
                const ps = document.getElementById('ppm-slider');
                const pi = document.getElementById('ppm-input');
                if (ps) ps.value = Math.max(-100, Math.min(100, p.ppm));
                if (pi) pi.value = p.ppm;
                setTimeout(() => { try { API.sdr.setFreqCorrection(p.ppm); } catch {} }, 0);
            }

            // VFO count (1-4). Applied BEFORE per-VFO settings.
            if (p.vfoCount !== undefined) {
                console.log(`[DashboardPrefs] restore vfoCount=${p.vfoCount}`);
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
                console.log(`[DashboardPrefs] restore vfoOffsets=`, p.vfoOffsets);
                p.vfoOffsets.forEach((off, i) => {
                    if (app._vfos[i] && typeof off === 'number') {
                        app._setVfoOffset(i, off);
                    }
                });
                // Having saved offsets means THIS client owns its VFO cursor
                // (the user previously tuned). Mark it so the periodic WS
                // status broadcast (!this._vfoUserTouched in app.js) does NOT
                // overwrite the restored offsets with the stale GLOBAL
                // vfo_offset_hz (typically 0) right after a page refresh.
                app._vfoUserTouched = true;
                console.log(`[DashboardPrefs] set _vfoUserTouched=true (owns cursor)`);
            }
            // Active VFO
            if (p.activeVfo !== undefined && typeof app.selectActiveVfo === 'function') {
                console.log(`[DashboardPrefs] restore activeVfo=${p.activeVfo}`);
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
                console.log(`[DashboardPrefs] restore masterVolume=${p.masterVolume}`);
                try { audio.setVolume(p.masterVolume); } catch {}
            }
            // Global mute
            if (p.muted !== undefined && typeof audio.setMuted === 'function') {
                console.log(`[DashboardPrefs] restore muted=${p.muted}`);
                try { audio.setMuted(p.muted); } catch {}
            }
            // Per-VFO mute + speaker. Also sync the DOM dropdown so the UI
            // matches the restored routing (the <select> value is independent
            // of the Web Audio graph, which only exists while listening).
            if (Array.isArray(p.perVfo)) {
                // Resolve App via Script Scope (see note in apply()).
                const appRef = (typeof App !== 'undefined') ? App : global.App;
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

    // Log on load so the console shows exactly which version is running
    // (makes it easy to spot a stale cached copy).
    console.log(`%c[DashboardPrefs v${VERSION}] script loaded`, 'color:#4af');

    global.DashboardPrefs = Prefs;
})(window);