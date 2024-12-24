import { Dialog } from "@web/core/dialog/dialog";
import { cookie } from "@web/core/browser/cookie";
import { useService } from "@web/core/utils/hooks";

import { Component, useState, onWillStart, onWillRender } from "@odoo/owl";


export class ManagePreferencesDialog extends Component {
    static components = { Dialog };
    static template = 'runbot.ManagePreferencesDialog';
    
    setup() {
        const projectId = document.head.querySelector('[name=runbot-project-id]').content;
        this.projectId = projectId && Number(projectId);
        this.filterModes = [
            {value: 'all', label: 'All'},
            {value: 'sticky', label: 'Sticky only'},
            {value: 'nosticky', label: 'Dev only'},
        ]
        this.originalState = {
            filter_mode: cookie.get('filter_mode') || 'all',
            category: Number(cookie.get('category') || '1'),
        }
        this.dirtyTriggers = false;
        this.clearTriggerCookie = false;
        this.orm = useService('orm');

        this.state = useState({
            categories: [],
            categoryById: {},
            triggersByCategory: {},

            selectedCategory: this.originalState.category,
            selectedFilterMode: this.originalState.filter_mode,
        });

        onWillStart(async () => {
            this.state.categories = await this.orm.searchRead(
                'runbot.category',
                [],
                ['id', 'name'],
            );
            this.state.categoryById = Object.fromEntries(
                this.state.categories.map(c => [c.id, c])
            );
            const triggers = await this.orm.searchRead(
                'runbot.trigger',
                [['project_id', '=', this.projectId || false]],
                ['id', 'name', 'category_id', 'hide'],
            );
            this.state.triggersByCategory = triggers.reduce(
                (agg, trigger) => {
                    if (!agg[trigger.category_id[0]]) {
                        agg[trigger.category_id[0]] = []
                    }
                    agg[trigger.category_id[0]].push(trigger);
                    return agg;
                }, {}
            );
            const activeTriggerCookie = cookie.get(`trigger_display_${this.projectId}`);
            const activeTriggersFromCookies = activeTriggerCookie && (
                activeTriggerCookie.split('-').map(Number)
            );
            Array.from(Object.values(this.state.triggersByCategory)).flat().forEach(
                trigger => {
                    trigger.active = activeTriggersFromCookies ? activeTriggersFromCookies.includes(trigger.id) : !trigger.hide;
                }
            );
        });
    }

    get _allTriggers() {
        return Array.from(Object.values(this.state.triggersByCategory)).flat();
    }

    save() {
        const cookieName = `trigger_display_${this.projectId}`;
        if (this.clearTriggerCookie) {
            cookie.delete(cookieName);
        } else if (this.dirtyTriggers) {
            cookie.set(cookieName, this._computeTriggerCookie());
        }
        cookie.set('filter_mode', this.state.selectedFilterMode);
        cookie.set('category', this.state.selectedCategory);
        location.reload();
    }

    _computeTriggerCookie() {
        return this._allTriggers.filter(t => t.active).map(({ id }) => id).sort().join('-');
    }

    selectFilterMode({ value }) {
        this.state.selectedFilterMode = value;
    }

    selectCategory({ id }) {
        this.state.selectedCategory = id;
    }
    
    toggleTrigger(trigger) {
        trigger.active = !trigger.active;
        this.dirtyTriggers = true;
        this.clearTriggerCookie = false;
    }

    resetTriggers() {
        this._allTriggers.forEach(
            trigger => trigger.active = !trigger.hide
        );
        this.clearTriggerCookie = true;
    }

    allTriggers() {
        this._allTriggers.forEach(t => t.active = true);
        this.dirtyTriggers = true;
        this.clearTriggerCookie = false;
    }

    noTriggers() {
        this._allTriggers.forEach(t => t.active = false);
        this.dirtyTriggers = true;
        this.clearTriggerCookie = false;
    }
}
