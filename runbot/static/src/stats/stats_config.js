/** @odoo-module **/

import { Component } from '@odoo/owl';

import { useConfig } from '@runbot/stats/use_config';


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
        this.config = useConfig();
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
};
