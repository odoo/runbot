/** @odoo-module **/

import { reactive, useEffect, useState, useEnv, useSubEnv } from '@odoo/owl';


/**
 * Search configuration for the stat page.
 */
export class Config {
    constructor({
        limit = 25, center_build_id = '0', key_category = 'module_loading_queries',
        mode = 'normal', nb_dataset = 20, display_aggregate = 'none', visible_keys = '',
    }) {
        this.limit = limit;
        this.center_build_id = center_build_id;
        this.key_category = key_category;
        this.mode = mode;
        this.nb_dataset = nb_dataset;
        this.display_aggregate = display_aggregate;
        this.visible_keys = visible_keys;
    }

    /**
     * Parses the url hash to fetch the default configuration.
     *
     * @returns new configuration from current url hash
     */
    static fromSearchParams() {
        const config = Object.fromEntries(new URLSearchParams(window.location.hash.substring(1)));
        const numberKeys = ['limit', 'nb_dataset'];
        numberKeys.forEach((key) => {
            if (!(key in config)) {
                return;
            }
            const sVal = config[key];
            if (isNaN(sVal)) {
                delete config[key];
            } else {
                config[key] = parseInt(sVal);
            }
        })
        return new Config(config);
    }

    /**
     * Returns the config a an URLSearchParams object.
     *
     * @return {URLSearchParams}
     */
    asSearchParams() {
        return new URLSearchParams({...this});
    }

    /**
     * Updates the url hash according to the current state of the config.
     */
    updateSearchParams() {
        window.location.hash = `#${new URLSearchParams({...this}).toString()}`
    }

    /**
     * Gets a set of keys that should trigger a refetch, other keys are treated as
     * display settings.
     *
     * @returns {string[]} set of keys that should trigger a refetch
     */
    getRefetchKeys() {
        return [
            'limit', 'center_build_id', 'key_category',
        ];
    }

    /**
     * Gets a set of keys that should trigger a chart update _only_.
     *
     * @returns {string[]} set of keys that should trigger a chart update
     */
    getChartUpdateKeys() {
        return ['mode', 'nb_dataset', 'display_aggregate', 'visible_keys'];
    }

    /**
     * Gets a set of keys that should not be kept when changing trigger.
     *
     * @returns {string[]} set of keys to remove when changing trigger.
     */
    getTriggerSpecificKeys() {
        return ['center_build_id', 'key_category', 'visible_keys'];
    }

    /**
     * Gets the visible keys as an array instead of string.
     *
     * @returns {string[]} list of visible keys
     */
    getVisibleKeys() {
        return this.visible_keys.split('-');
    }

    /**
     * Sets the given visible keys as visible keys.
     * 
     * @param {string[]} keys the keys to add
     */
    pushVisibleKeys(keys) {
        this.visible_keys = keys.join('-');
    }

    /**
     * Toggles the given key from visible keys.
     *
     * @param {string} key the key to toggle
     */
    toggleVisibleKey(key) {
        const keys = this.getVisibleKeys();
        const keyIdx = keys.indexOf(key);
        if (keyIdx === -1) {
            keys.push(key);
        } else {
            keys.splice(keyIdx, 1);
        }
        this.pushVisibleKeys(keys);
    }
}

/**
 * Gets the current configuration note that the component is not made reactive directly.
 * If the configuration is non existant (parent element) a config is created through `fromSearchParams`.
 *
 * @returns {Config} config
 */
export const useConfig = (makeReactive = true) => {
    const env = useEnv();
    if (env.statsConfig) {
        if (makeReactive) {
            return useState(env.statsConfig);
        }
        return env.statsConfig;
    }
    const statsConfig = reactive(Config.fromSearchParams());
    useSubEnv({
        statsConfig,
    });
    if (makeReactive) {
        return useState(statsConfig);
    }
    return statsConfig;
}


/**
 * @callback OnConfigChangeCallback
 *
 * @param {Config} config
 */
/**
 * Calls the callback any time the config changes.
 *
 * @param {OnConfigChangeCallback} callback method to call back
 * @param {Boolean} forRefetch if the callback needs to be called for data refresh only
 */
export const onConfigChange = (callback, forRefetch = false) => {
    const config = useConfig();
    const keys = forRefetch ? config.getRefetchKeys() : Object.keys(config);
    useEffect(
        () => callback(config),
        () => keys.map(k => config[k]),
    );
}
