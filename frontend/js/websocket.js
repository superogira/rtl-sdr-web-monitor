/**
 * WebSocket Client for RTL-SDR Monitor
 */
const WS = {
    ws: null,
    url: null,
    listeners: {},
    reconnectTimer: null,
    heartbeatTimer: null,
    _wasConnected: false,  // tracks if we've connected at least once (for reconnect detection)

    connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.url = `${protocol}//${window.location.host}/ws`;
        this._doConnect();
    },

    _doConnect() {
        try {
            this.ws = new WebSocket(this.url);
            // Receive binary frames as ArrayBuffer so we can read spectrum data
            // directly as a typed array (no base64 + atob + per-byte loop).
            this.ws.binaryType = 'arraybuffer';

            this.ws.onopen = () => {
                console.log('🔌 WebSocket connected');
                this._updateStatus('🟢');
                // If this isn't the first connection, it's a reconnect —
                // notify listeners so they can refresh state (e.g. reload
                // the latest recordings that may have been missed during
                // the disconnect gap).
                if (this._wasConnected) {
                    console.log('🔄 WebSocket reconnected — refreshing state');
                    this._emit('reconnected', {});
                } else {
                    this._wasConnected = true;
                }
                this._emit('connected', {});
                this.startHeartbeat();
            };

            this.ws.onmessage = (event) => {
                // Binary frame: 2-byte type tag + payload.
                //   \x01\x00 = spectrum (int16 LE values, raw dB)
                if (event.data instanceof ArrayBuffer) {
                    const view = new DataView(event.data);
                    const tag = view.getUint16(0, true);
                    if (tag === 0x0001) {
                        // Spectrum: read int16 array directly from the buffer,
                        // skipping the 2-byte tag. Much faster than the old
                        // base64 → atob → charCodeAt loop.
                        const spectrum = new Int16Array(event.data, 2);
                        this._emit('spectrum', spectrum);
                    }
                    return;
                }
                // Text frame: JSON {type, data}
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type && msg.data !== undefined) {
                        this._emit(msg.type, msg.data);
                    }
                } catch (e) {
                    console.error('WS parse error:', e);
                }
            };

            this.ws.onclose = () => {
                console.log('🔌 WebSocket disconnected');
                this._updateStatus('🔴');
                this._emit('disconnected', {});
                this.scheduleReconnect();
            };

            this.ws.onerror = (err) => {
                console.error('WS error:', err);
                this._updateStatus('🔴');
            };
        } catch (e) {
            console.error('WS connect error:', e);
            this.scheduleReconnect();
        }
    },

    _emit(event, data) {
        const handlers = this.listeners[event];
        if (handlers) {
            handlers.forEach(cb => {
                try { cb(data); } catch (e) { console.error('WS handler error:', e); }
            });
        }
    },

    on(event, callback) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(callback);
    },

    off(event, callback) {
        if (!this.listeners[event]) return;
        this.listeners[event] = this.listeners[event].filter(cb => cb !== callback);
    },

    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(typeof data === 'string' ? data : JSON.stringify(data));
        }
    },

    startHeartbeat() {
        this.stopHeartbeat();
        this.heartbeatTimer = setInterval(() => {
            this.send('ping');
        }, 30000);
    },

    stopHeartbeat() {
        if (this.heartbeatTimer) {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
    },

    scheduleReconnect() {
        if (this.reconnectTimer) return;
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            console.log('🔌 Reconnecting WebSocket...');
            this._doConnect();
        }, 3000);
    },

    /** Called after a successful reconnect so listeners can refresh
     *  state that may have been missed while the WS was down (e.g.
     *  reload the latest recordings feed to pick up any events that
     *  were emitted during the disconnect gap). */
    _notifyReconnected() {
        this._emit('reconnected', {});
    },

    _updateStatus(icon) {
        const el = document.getElementById('ws-status');
        if (el) {
            el.textContent = 'WS: ' + icon;
            el.style.color = icon === '🟢' ? '#4f4' : '#f66';
        }
    },

    disconnect() {
        this.stopHeartbeat();
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    },
};