/**
 * Selected Device — per-tab device selection for multi-device live listening.
 *
 * Instead of calling the global /api/sdr/devices/:id/activate (which changes
 * the device for EVERYONE), each browser tab picks its own device via this
 * object. The id is appended to WebSocket URLs (?device_id=...) and (optionally)
 * SDR API calls so the backend resolves the right per-device runtime.
 */
window.SelectedDevice = {
    _id: null,

    /** Set the device this tab uses for live listening. */
    set(id) {
        this._id = id || null;
        try { sessionStorage.setItem('rtl_sdr_selected_device', id || ''); } catch {}
        // Notify listeners (websocket.js, audio.js, app.js)
        window.dispatchEvent(new CustomEvent('selected-device-changed', { detail: { id: this._id } }));
    },

    /** Get the device this tab uses for live listening (null = default/active). */
    get() {
        if (this._id) return this._id;
        try {
            const stored = sessionStorage.getItem('rtl_sdr_selected_device');
            if (stored) { this._id = stored; return stored; }
        } catch {}
        return null;
    },
};