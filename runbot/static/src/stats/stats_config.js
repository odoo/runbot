/** @odoo-module **/

import { Component, useEffect, useState } from '@odoo/owl';

import { useConfig } from '@runbot/stats/use_config';
import { getBundleName, populateCache } from '@runbot/stats/cache';
import { debounce, randomColor } from '@runbot/utils';


/**
 * @typedef Bundle
 *
 * @property {Number} id
 * @property {String} name
 */

export class StatsConfig extends Component {
    static template = 'runbot.StatsConfig';
    static props = {
        bundle: {
            type: Object,
            shape: {
                id: { type: Number },
                name: { type: String },
            },
        },
        trigger: {
            type: Object,
            shape: {
                id: { type: Number },
                name: { type: String },
            },
        },
        project: {
            type: Object,
            shape: {
                id: { type: Number },
                name: { type: String },
            },
        },
        stats_categories: { type: Array, element: String },
        triggers_by_category: {
            type: Object,
            values: {
                type: Array,
                element: {
                    type: Object,
                    shape: {
                        id: { type: Number },
                        slug: { type: String },
                        name: { type: String },
                    }
                }
            }
        },
    };

    setup() {
        this._origFetchBundles = this._fetchBundles.bind(this);
        this._fetchBundles = debounce(this._fetchBundles.bind(this), 500);
        this.config = useConfig();
        this.requestId = 0;

        this.state = useState({
            search: '',
            searchLoading: false,
            searchResult: [],
        });

        useEffect(
            () => this.fetchBundles(),
            () => [this.state.search],
        );
    }

    /**
     * List of active bundles
     */
    get bundles() {
        return [
            this.props.bundle,
            ...this.config.getBundles().map(
                (id) => ({ id, name: getBundleName(id) }),
            ),
        ];
    }

    /**
     * List of search bundles without the active bundles.
     */
    get searchBundles() {
        const activeBundles = this.bundles;
        return this.state.searchResult.filter(
            (sr) => !activeBundles.find(b => b.id === sr.id)
        );
    }

    /**
     * Gets a color for the given bundle.
     *
     * @param {Bundle} bundle the bundle
     * @returns {String} color as hexcode
     */
    bundleColor(bundle) {
        return randomColor(bundle.name);
    }

    /**
     * Fetches the bundle according to the current search state.
     * If the search is emtpy the result is changed directly, otherwise a debounce
     * happend between this call and the actual result being shown on screen,
     * however this function is responsible for toggling the visual loading state.
     */
    fetchBundles() {
        this.state.searchLoading = true;
        if (this.state.search.trim() === '') {
            this._origFetchBundles();
        } else {
            this._fetchBundles();
        }
    }

    /**
     * Fetches bundles from the backend according to the search state.
     * If the search is empty, the state is reset, the lookup result is not kept if this is not the latest call
     * to this method.
     * Regardless if bundles are kept from the search or not the result is used to populate the name cache.
     */
    async _fetchBundles() {
        const requestId = ++this.requestId;
        const search = this.state.search.trim();
        if (!search.length) {
            this.state.searchLoading = false;
            this.state.searchResult = [];
            return;
        }
        const result = await fetch(`/runbot/bundles_json/${this.props.project.id}/search/${search}/?limit=10`);
        const resultJson = await result.json();
        populateCache(resultJson);
        if (requestId === this.requestId) {
            this.state.searchLoading = false;
            this.state.searchResult = resultJson;
        }
    }

    /**
     * Called when the trigger selection is changed.
     * This changes the config to remove trigger specific keys and redirects
     * the user towards the same page with the new trigger.
     *
     * @param {Event} event the event
     */
    onChangeTrigger(event) {
        const { origin, pathname, search } = window.location;
        const [_, bundle] = /\/runbot\/stats\/(.+)\/.+/.exec(pathname);
        const newParams = this.config.asSearchParams();
        this.config.getTriggerSpecificKeys().forEach(
            (key) => newParams.delete(key)
        );
        window.location.href = `${origin}/runbot/stats/${bundle}/${event.target.value}${search}#${newParams.toString()}`;
    }

    /**
     * Called when adding a bundle to the list of bundles to include.
     *
     * @param {Bundle} bundle the bundle
     */
    onClickAddBundle(bundle) {
        this.config.toggleBundle(bundle.id);
    }

    /**
     * Called when removing a bundle from the list of bundles to include.
     *
     * @param {Bundle} bundle the bundle
     */
    onClickRemoveBundle(bundle) {
        this.config.toggleBundle(bundle.id);
    }
};
