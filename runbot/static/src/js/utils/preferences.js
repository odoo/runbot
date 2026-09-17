/**
 * Preferences, stored as cookies to make them available to the server on the
 * next request.
 */
class PreferencesManager {
    // Preferences are meant to be kept "forever", the browser will cap it anyway.
    static maxAge = 10 * 365 * 24 * 60 * 60;
    static path = "/";

    /**
     * @param {string} key
     * @param {string} value
     */
    set(key, value) {
        this._write(key, value, PreferencesManager.maxAge);
    }

    /**
     * @param {string} key
     */
    remove(key) {
        this._write(key, "", 0);
    }

    /**
     * @param {string} key
     * @param {string} value
     * @param {number} maxAge
     */
    _write(key, value, maxAge) {
        const parts = [
            `${encodeURIComponent(key)}=${encodeURIComponent(value)}`,
            `path=${PreferencesManager.path}`,
            `max-age=${maxAge}`,
        ];
        document.cookie = parts.join("; ");
    }
}

export const preferences = new PreferencesManager();
