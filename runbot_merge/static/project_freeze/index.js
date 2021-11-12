odoo.define('runbot_merge.index', function (require) {
"use strict";
const FormController = require('web.FormController');
const FormView = require('web.FormView');
const viewRegistry = require('web.view_registry');

/**
 * Attept at a "smart" controller for the freeze wizard: keeps triggering
 * onchange() on the form in order to try and update the error information, as
 * some of the "errors" are not under direct operator control. Hopefully this
 * allows the operator to just keep the wizard open and wait until the error
 * messages disappear so they can proceed.
 */
const FreezeController = FormController.extend({
    async _checkState() {
        const record = this.model.get(this.handle)
        const requiredPrIds = record.data.required_pr_ids;

        // we're inside the form's mutex, so can use `_applyChange` directly
        const changed = await this.model._applyChange(this.handle, {
            required_pr_ids: {
                operation: 'REPLACE_WITH',
                ids: requiredPrIds.res_ids,
            }
        });
        // not sure why we need to wait for the round *after* the error update
        // notification, but even debouncing the rest of the method is not
        // sufficient (so it's not just a problem of being behind the mutex,
        // there's something wonky going on)
        if (!this._updateNext) {
            this._updateNext = changed.includes('errors');
            return;
        }

        this._updateNext = false;
        for(const p of requiredPrIds.data) {
            this.renderer.updateState(p.id, {fieldNames: ['state_color']});
        }
        this.renderer.updateState(record, {fieldNames: ['errors', 'required_pr_ids']});
    },
    /**
     * @override
     */
    async start(...args) {
        const checker = async () => {
            if (this.isDestroyed()) { return; }
            await this.model.mutex.exec(() => this._checkState());
            setTimeout(checker, 1000);
        };
        const started = await this._super(...args);
        const _ = checker();
        return started;
    },
});

viewRegistry.add('freeze_wizard', FormView.extend({
    config: Object.assign({}, FormView.prototype.config, {
        Controller: FreezeController,
    })
}));
});

