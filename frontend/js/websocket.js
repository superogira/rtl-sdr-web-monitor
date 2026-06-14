/**
 * WebSocket Client for RTL-SDR Monitor
 */
const WS = {
    ws: null,
    url: null,
    listeners: {},
    reconnectTimer: null,
    heartbeatTimer: null,

    connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.url = `${protocol}//${window.location.host}/ws`;
        this._doConnect();
    },

    _doConnect() {
        try {
            this.ws = new WebSocket(this.url);

            this.ws.onopen = () => {
                console.log('🔌 WebSocket connected');
                this._updateStatus('🟢');
                this._emit('connected', {});
                this.startHeartbeat();
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    // msg = { type: "status"|"spectrum"|"new_recording", data: {...} }
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