/** @odoo-module **/

import { Component } from '@odoo/owl';
import { useConfig, onConfigChange } from '@runbot/stats/use_config';


export class UrlUpdater extends Component {
    static template = 'runbot.UrlUpdater';
    static components = {};

    setup() {
        onConfigChange((config) => {
            config.updateSearchParams();
        });
    }
}
