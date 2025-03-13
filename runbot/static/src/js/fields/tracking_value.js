/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { Message } from "@mail/core/common/message";
import { diff_match_patch } from "@runbot/libs/diff_match_patch/diff_match_patch";
import { DiffDisplay } from './diff_display';

patch(Message, {
    components: {...Message.components, DiffDisplay},
});

patch(Message.prototype, {
    setup() {
        super.setup(...arguments);
        this.kept = false;
    },
    isMultiline(trackingValue) {
        const oldValue = trackingValue.oldValue.value;
        const newValue = trackingValue.newValue.value;
        return ((oldValue && typeof oldValue=== 'string' && oldValue.includes('\n')) && (newValue && typeof oldValue=== 'string' && newValue.includes('\n')))
    },
    toggleKept() {
        this.kept = !this.kept;
    },
    copyOldToClipboard(trackingValue) {
        return function () {
            navigator.clipboard.writeText(trackingValue.oldValue.value);
        };
    },
    copyNewToClipboard(trackingValue) {
        return function () {
            navigator.clipboard.writeText(trackingValue.newValue.value);
        };
    },
});
