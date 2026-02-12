import { FormController } from "@web/views/form/form_controller";
import { patch } from "@web/core/utils/patch";


patch(FormController.prototype, {
    // Prevent saving on tab switching
    beforeVisibilityChange: () => {},

    // Prevent closing page with dirty fields
    async beforeUnload(ev) {
        if (await this.model.root.isDirty()) {
            ev.preventDefault();
            ev.returnValue = "Unsaved changes";
        } else {
            super.beforeUnload(ev);
        }
    },
});
