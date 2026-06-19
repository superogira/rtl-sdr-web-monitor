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
        /**
         * Set the VFO (listening) frequency by absolute Hz.
         * Available to all users (admin + user). Moves the listening cursor
         * within the passband without retuning the SDR (waterfall stays still).
         */
        async setVFO(freq) {
            return API.request('POST', '/api/sdr/vfo', { frequency: freq });
        },
        /**
         * Set the VFO passband margin ratio (admin only).
         * Controls how far the VFO can tune from center: max_offset = sample_rate * ratio.
         * Accepts 0.40–0.49. Lower = safer (less aliasing near band edge),
         * higher = wider tunable range.
         */
        async setVFOMargin(ratio) {
            return API.request('POST', '/api/sdr/vfo-margin', { ratio: parseFloat(ratio) });
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
        async getRecordingConfig() {
            return API.request('GET', '/api/sdr/recording-config');
        },
        async setRecordingConfig(data) {
            return API.request('POST', '/api/sdr/recording-config', data);
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
        /**
         * Get whether manual recording is enabled for regular (role=user) users.
         * Admins always have access (admin_bypass=true).
         */
        async getRecordingEnabled() {
            return API.request('GET', '/api/sdr/recording-enabled');
        },
        /**
         * Enable/disable manual recording for regular users (admin only).
         */
        async setRecordingEnabled(enabled) {
            return API.request('POST', '/api/sdr/recording-enabled', { enabled: !!enabled });
        },
        // === SDR Device registry (admin) ===
        devices: {
            async listTypes() {
                return API.request('GET', '/api/sdr/devices/types');
            },
            async list() {
                return API.request('GET', '/api/sdr/devices');
            },
            async get(id) {
                return API.request('GET', `/api/sdr/devices/${id}`);
            },
            async create(data) {
                return API.request('POST', '/api/sdr/devices', data);
            },
            async update(id, data) {
                return API.request('PUT', `/api/sdr/devices/${id}`, data);
            },
            async remove(id) {
                return API.request('DELETE', `/api/sdr/devices/${id}`);
            },
            async activate(id) {
                return API.request('POST', `/api/sdr/devices/${id}/activate`);
            },
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
            if (params.search) qs.set('search', params.search);
            if (params.frequency_mhz != null) qs.set('frequency_mhz', params.frequency_mhz);
            try {
                const resp = await API.request('GET', `/api/recordings?${qs.toString()}`);
                return resp;  // {items, total, limit, offset}
            } catch { return { items: [], total: 0, limit: 50, offset: 0 }; }
        },
        async listFrequencies(params = {}) {
            const qs = new URLSearchParams();
            if (params.source) qs.set('source', params.source);
            try {
                const resp = await API.request('GET', `/api/recordings/meta/frequencies?${qs.toString()}`);
                return resp;  // {items: [{frequency_mhz, count, min_mhz, max_mhz}], tolerance_khz}
            } catch { return { items: [], tolerance_khz: 5 }; }
        },
        async get(id) {
            return API.request('GET', `/api/recordings/${id}`);
        },
        async delete(id) {
            return API.request('DELETE', `/api/recordings/${id}`);
        },
        /**
         * Bulk delete recordings by IDs (admin only).
         * @param {string[]} ids - Recording IDs to delete
         * @returns {Promise<{success, requested_count, deleted_count, deleted_ids, not_found_ids}>}
         */
        async bulkDelete(ids) {
            return API.request('POST', '/api/recordings/bulk-delete', { ids });
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
        /**
         * Set the user's verdict on how good a recording's transcription is.
         * @param {string} id - Recording ID
         * @param {number} rating - 1..6 (1=แย่มาก, 6=ดีที่สุด)
         */
        async setQuality(id, rating) {
            return API.request('POST', `/api/recordings/${id}/quality`, { rating });
        },
        /**
         * Aggregate STT processing-time stats (minutes) for charts.
         * @param {object} params - { start: ISO, end: ISO }
         */
        async sttStats(params = {}) {
            const qs = new URLSearchParams();
            if (params.start) qs.set('start', params.start);
            if (params.end) qs.set('end', params.end);
            try {
                return await API.request('GET', `/api/recordings/meta/stt-stats?${qs.toString()}`);
            } catch { return { by_provider: [], by_whisper_model: [] }; }
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

    // Recording Jobs (scheduled/always-on background recording)
    recordingJobs: {
        async list() {
            return API.request('GET', '/api/recording-jobs');
        },
        async availableDevices() {
            return API.request('GET', '/api/recording-jobs/available-devices');
        },
        async create(data) {
            return API.request('POST', '/api/recording-jobs', data);
        },
        async update(id, data) {
            return API.request('PUT', `/api/recording-jobs/${id}`, data);
        },
        async remove(id) {
            return API.request('DELETE', `/api/recording-jobs/${id}`);
        },
        async setEnabled(id, enabled) {
            return API.request('POST', `/api/recording-jobs/${id}/enabled`, { enabled: !!enabled });
        },
        async getServerTimezone() {
            return API.request('GET', '/api/recording-jobs/server-timezone');
        },
        async runtimeStatus() {
            return API.request('GET', '/api/recording-jobs/runtime-status');
        },
        async setServerTimezone(timezone) {
            return API.request('PUT', '/api/recording-jobs/server-timezone', { timezone });
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
