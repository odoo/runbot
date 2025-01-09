/** @odoo-module **/

/**
 * Creates a debounced version of a function.
 * 
 * @template {Function} T Initial type of fn
 * @param {T} fn The function to debounce
 * @param {Number} delay The number of milliseconds to debounce
 * 
 * @return {T} The debounced function
 */
export const debounce = (fn, delay = 500) => {
    let handle;
    return (...args) => {
        clearTimeout(handle);
        handle = setTimeout(() => {
            fn(...args);
        }, delay);
    }
}

/**
 * Deterministically determine a color for a given object.
 * The object is stringified then hashed into a color index.
 *
 * @param {Object} any object to hash
 */
export const randomColor = (name) => {
    const colors = ['#004acd', '#3658c3', '#4a66ba', '#5974b2', '#6581aa', '#6f8fa3', '#7a9c9d', '#85a899', '#91b596', '#a0c096', '#fdaf56', '#f89a59', '#f1865a', '#e87359', '#dc6158', '#ce5055', '#bf4150', '#ad344b', '#992a45', '#84243d'];
    let sum = 0;
    const str = JSON.stringify(name);
    for (let i = 0; i < str.length; i++) {
        sum += str.charCodeAt(i);
    }
    return colors[sum % colors.length];
}

/**
 * Filters an object according to some given keys.
 *
 * @param {Object} obj object to filter
 * @param {string[]} keys keys to keep
 */
export const filterKeys = (obj, keys) => {
    return Object.fromEntries(keys.map(k => [k, obj[k]]));
}
