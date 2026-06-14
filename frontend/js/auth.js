/**
 * Authentication Manager for RTL-SDR Monitor
 */
const Auth = {
    /**
     * Login with username and password
     */
    async login(username, password) {
        try {
            const data = await API.auth.login(username, password);
            localStorage.setItem('access_token', data.access_token);
            localStorage.setItem('refresh_token', data.refresh_token);
            localStorage.setItem('user', JSON.stringify(data.user));
            return { success: true, user: data.user };
        } catch (error) {
            return { success: false, error: error.message };
        }
    },

    /**
     * Logout and clear tokens
     */
    logout() {
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        localStorage.removeItem('user');
        window.location.href = '/login';
    },

    /**
     * Check if user is authenticated
     */
    isAuthenticated() {
        return !!localStorage.getItem('access_token');
    },

    /**
     * Get current user info from localStorage
     */
    getUser() {
        const userStr = localStorage.getItem('user');
        if (userStr) {
            try {
                return JSON.parse(userStr);
            } catch {
                return null;
            }
        }
        return null;
    },

    /**
     * Check if current user is admin
     */
    isAdmin() {
        const user = this.getUser();
        return user && user.role === 'admin';
    },

    /**
     * Get access token
     */
    getToken() {
        return localStorage.getItem('access_token');
    },

    /**
     * Require authentication - redirect to login if not authenticated
     */
    requireAuth() {
        if (!this.isAuthenticated()) {
            window.location.href = '/login';
            return false;
        }
        return true;
    },

    /**
     * Update user info from server
     */
    async refreshUserInfo() {
        try {
            const user = await API.auth.getMe();
            localStorage.setItem('user', JSON.stringify(user));
            return user;
        } catch {
            return null;
        }
    },
};