/** @odoo-module **/

import { Component, useState } from '@odoo/owl';

import { useBus } from '@runbot/stats/use_bus';
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
    };

    setup() {
        this.state = useState({
            loading: true,
        })
        this.config = useConfig();

        useBus(this.env.bus, 'start-loading', () => this.state.loading = true);
        useBus(this.env.bus, 'stop-loading', () => this.state.loading = false);
    }

    onClickPrevious() {
        this.env.bus.trigger('click-previous', {});
    }
    
    onClickNext() {
        this.env.bus.trigger('click-next', {});
    }
};
