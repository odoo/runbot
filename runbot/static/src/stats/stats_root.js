/** @odoo-module **/

import { Component, whenReady, App } from '@odoo/owl';
import { getTemplate } from '@web/core/templates';

import { StatsConfig } from '@runbot/stats/stats_config';
import { StatsChart } from '@runbot/stats/stats_chart';
import { useConfig } from '@runbot/stats/use_config';
import { UrlUpdater } from '@runbot/stats/url_updater';


export class StatsRoot extends Component {
    static template = 'runbot.StatsRoot';
    static components = {StatsConfig, StatsChart, UrlUpdater};
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
        // Initialize shared configuration for children components.
        useConfig(false);
    }
}

whenReady(() => {
    const rootElement = document.getElementById('wrapwrap');
    if (!rootElement || !globalThis.__runbot_stats_values) {
        return console.error('Could not initialize stats, wrapwrap not found');
    }
    rootElement.textContent = '';
    const app = new App(StatsRoot, { props: globalThis.__runbot_stats_values, getTemplate });
    app.mount(rootElement);
});
