/**
 * Returns a regex that will match the group name to the id of the slugged string.
 *
 * @param {string} groupName The name of the group.
 * @returns {string} The regex slice for unslugging.
 */
export function unslug_re(groupName) {
    return `(?:(?:\\w{1,2}|\\w[A-Za-z0-9-_]+?\\w)-)?(?<${groupName}>-?\\d+)(?=$|\\/|#|\\?)`
};

/**
 * Returns a slug for a given name and id.
 *
 * @param {string} name Name of the object
 * @param {Number} id Id of the object
 * @returns The slugged string
 */
export function slugify({name, id}) {
    const slugged = name
        .replaceAll(/\W+/g, '-')
        .replaceAll(/^-*|-*$/g, '')
        .trim()
        .toLowerCase();
    return `${slugged}-${id}`;
};

/**
 * Returns the cookie for a given key.
 *
 * @param {string} str The key of the cookie
 * @returns The value of the cookie or undefined if the cookie is not set.
 */
export function getCookie(str) {
    const parts = document.cookie.split("; ");
    for (const part of parts) {
        const [key, value] = part.split(/=(.*)/);
        if (key === str) {
            return value || '';
        }
    }
}

/**
 * Set a cookie for all paths on the current domain.
 */
export function setCookie(key, value, ttl = 24 * 60 * 60 * 365) {
    document.cookie = [
        `${key}=${value}`,
        'path=/',
        `max-age=${ttl}`,
    ].join('; ');
}

/**
 * Returns the theme requested by the browser.
 */
export function getAutoTheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * Switches to the given them.
 */
export function switchTheme(theme) {
    if (!['light', 'dark', 'red404', 'auto'].includes(theme)) {
        return;
    }
    setCookie('colorScheme', theme);
    if (theme === 'auto') {
        theme = getAutoTheme();
    }
    document.documentElement.dataset.bsTheme = theme;
}
