/**
 * API Client for RTL-SDR Monitor
 */
const API = {
    baseUrl: window.location.origin,
    token: null,
    _refreshing: null, // prevent concurrent refreshes

    init() {
        this.token = localStorage.getItem('access_token');
    },

    getHeaders() {
        const headers = { 'Content-Type': 'application/json' };
        if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
        return headers;
    },

    async refreshToken() {
        if (this._refreshing) return this._refreshing;
        this._refreshing = (async () => {
            try {
                const refreshToken = localStorage.getItem('refresh_token');
                if (!refreshToken) throw new Error('No refresh token');
                const resp = await fetch(`${this.baseUrl}/api/auth/refresh`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ refresh_token: refreshToken }),
                });
                const data = await resp.json();
                if (!resp.ok) throw data;
                localStorage.setItem('access_token', data.access_token);
                localStorage.setItem('refresh_token', data.refresh_token);
                localStorage.setItem('user', JSON.stringify(data.user));
                this.token = data.access_token;
                return true;
            } catch (e) {
                // Refresh failed, force re-login
                localStorage.removeItem('access_token');
                localStorage.removeItem('refresh_token');
                localStorage.removeItem('user');
                window.location.href = '/login';
                throw e;
            } finally {
                this._refreshing = null;
            }
        })();
        return this._refreshing;
    },

    async request(method, url, body = null) {
        const opts = { method, headers: this.getHeaders() };
        if (body) opts.body = JSON.stringify(body);
        const resp = await fetch(`${this.baseUrl}${url}`, opts);

        // If 401, try to refresh token and retry once
        if (resp.status === 401 && this.token) {
            await this.refreshToken();
            opts.headers = this.getHeaders();
            const retryResp = await fetch(`${this.baseUrl}${url}`, opts);
            const retryData = await retryResp.json();
            if (!retryResp.ok) throw retryData;
            return retryData;
        }

        const data = await resp.json();
        if (!resp.ok) throw data;
        return data;
    },

    // Auth
    auth: {
        async login(username, password) {
            return API.request('POST', '/api/auth/login', { username, password });
        },
        async register(username, password, email = null) {
            return API.request('POST', '/api/auth/register', { username, password, email });
        },
        async getMe() {
            return API.request('GET', '/api/auth/me');
        },
    },

    // SDR
    sdr: {
        async connect(host = null, port = null) {
            const body = (host) ? { host, port: parseInt(port) || 1234 } : null;
            return API.request('POST', '/api/sdr/connect', body);
        },
        async setConnection(host, port) {
            return API.request('POST', '/api/sdr/connection', { host, port: parseInt(port) || 1234 });
        },
        async disconnect() {
            return API.request('POST', '/api/sdr/disconnect');
        },
        async status() {
            return API.request('GET', '/api/sdr/status');
        },
        async setFrequency(freq) {
            return API.request('POST', '/api/sdr/frequency', { frequency: freq });
        },
        async setSampleRate(rate) {
            return API.request('POST', '/api/sdr/sample-rate', { sample_rate: rate });
        },
        async setGain(gain) {
            return API.request('POST', '/api/sdr/gain', { gain });
        },
        async setMode(mode) {
            return API.request('POST', '/api/sdr/mode', { mode });
        },
        async setSquelch(level) {
            return API.request('POST', '/api/sdr/squelch', { level });
        },
        async setFFTSize(size) {
            return API.request('POST', '/api/sdr/fft-size', { fft_size: size });
        },
        async setBandwidth(bwHz) {
            return API.request('POST', '/api/sdr/bandwidth', { bandwidth: bwHz });
        },
        async setFreqCorrection(ppm) {
            return API.request('POST', '/api/sdr/freq-correction', { ppm });
        },
        async streamStart() {
            return API.request('POST', '/api/sdr/stream/start');
        },
        async streamStop() {
            return API.request('POST', '/api/sdr/stream/stop');
        },
        async recordStart() {
            return API.request('POST', '/api/sdr/record/start');
        },
        async recordStop() {
            return API.request('POST', '/api/sdr/record/stop');
        },
    },

    // Bookmarks (placeholder)
    bookmarks: {
        async list() {
            try { return await API.request('GET', '/api/bookmarks'); } catch { return []; }
        },
        async create(data) {
            return API.request('POST', '/api/bookmarks', data);
        },
        async delete(id) {
            return API.request('DELETE', `/api/bookmarks/${id}`);
        },
    },

    // Recordings (from DB)
    recordings: {
        async list(params = {}) {
            const qs = new URLSearchParams();
            qs.set('limit', params.limit || 50);
            if (params.offset) qs.set('offset', params.offset);
            if (params.source) qs.set('source', params.source);
            try {
                const resp = await API.request('GET', `/api/recordings?${qs.toString()}`);
                return resp;  // {items, total, limit, offset}
            } catch { return { items: [], total: 0, limit: 50, offset: 0 }; }
        },
        async get(id) {
            return API.request('GET', `/api/recordings/${id}`);
        },
        async delete(id) {
            return API.request('DELETE', `/api/recordings/${id}`);
        },
        async markRead(id) {
            return API.request('POST', `/api/recordings/${id}/read`);
        },
        async updateTranscript(id, transcript) {
            return API.request('PUT', `/api/recordings/${id}/transcript`, { transcript });
        },
        async transcribe(id) {
            return API.request('POST', `/api/recordings/${id}/transcribe`);
        },
    },

    // App Settings (global STT config, etc.)
    settings: {
        async getSTT() {
            return API.request('GET', '/api/settings/stt');
        },
        async updateSTT(data) {
            return API.request('PUT', '/api/settings/stt', data);
        },
    },
};

// === Helper Functions ===

/**
 * Format datetime string to user's timezone
 * @param {string} isoString - ISO datetime string (UTC or with timezone)
 * @param {string} userTimezone - User's timezone (e.g., "Asia/Bangkok")
 * @param {object} options - Intl.DateTimeFormat options
 * @returns {string} Formatted datetime string
 */
function formatDateTime(isoString, userTimezone, options = {}) {
    if (!isoString) return '--';
    
    const defaultOptions = {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        ...options
    };
    
    try {
        const date = new Date(isoString);
        return date.toLocaleString('th-TH', {
            ...defaultOptions,
            timeZone: userTimezone || 'Asia/Bangkok'
        });
    } catch (e) {
        console.error('Date formatting error:', e);
        return isoString;
    }
}

/**
 * Format time only (HH:MM)
 */
function formatTime(isoString, userTimezone) {
    return formatDateTime(isoString, userTimezone, {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    });
}

/**
 * Format date only (DD/MM/YYYY)
 */
function formatDate(isoString, userTimezone) {
    return formatDateTime(isoString, userTimezone, {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric'
    });
}

/**
 * Get user's timezone from localStorage
 */
function getUserTimezone() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
        try {
            const user = JSON.parse(userStr);
            return user.timezone || 'Asia/Bangkok';
        } catch (e) {
            console.error('Error parsing user from localStorage:', e);
        }
    }
    return 'Asia/Bangkok';
}

API.init();
