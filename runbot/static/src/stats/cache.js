/** @odoo-module **/


/**
 * @typedef Bundle
 *
 * @property {Number} id
 * @property {String} name
 */

const bundleNameCache = {}; // id to name

/**
 * Returns the name of the bundle according to the cache.
 *
 * @param {Number} bundleId id of the bundle
 */
export const getBundleName = (bundleId) => {
    return bundleNameCache[bundleId] || bundleId.toString();
}

/**
 * Populates the cache when a new list of bundle is loaded.
 *
 * @param {Bundle[]} bundles list of bundles
 */
export const populateCache = (bundles) => {
    bundles.forEach(({ id, name }) => bundleNameCache[id] = name);
}
