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

    // Recordings (placeholder)
    recordings: {
        async list(params = {}) {
            try { return await API.request('GET', `/api/recordings?limit=${params.limit || 20}`); } catch { return []; }
        },
    },
};

API.init();