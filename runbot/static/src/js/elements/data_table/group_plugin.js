import { DataTablePlugin } from "./plugin";

const GROUP_SELECTOR = ".table-group-divider";
const GROUP_HEADER_SELECTOR = `${GROUP_SELECTOR} > tr > th`;
const HIDDEN_GROUP_CLASS = "table-group-hidden";
const COLLAPSE_SELECTOR = "[data-toggle='table-group-collapse']";
// interactive content of a group header, keeping its own behaviour
const INTERACTIVE_SELECTOR = "a, button, input, label, select, textarea";

/**
 * Each `.table-group-divider` tbody collapses by clicking its header, or all of them at once with the
 * `table-group-collapse` button.
 */
export class GroupPlugin extends DataTablePlugin {
    connect() {
        this.dataTable.addEventListener("click", this._onClick);
        this.updateCollapseButton();
    }

    disconnect() {
        this.dataTable.removeEventListener("click", this._onClick);
    }

    get groups() {
        return [...this.dataTable.querySelectorAll(GROUP_SELECTOR)];
    }

    get isAllCollapsed() {
        return this.groups.every((group) => group.classList.contains(HIDDEN_GROUP_CLASS));
    }

    toggleAll() {
        const collapse = !this.isAllCollapsed;
        for (const group of this.groups) {
            group.classList.toggle(HIDDEN_GROUP_CLASS, collapse);
        }
        this.updateCollapseButton();
    }

    updateCollapseButton() {
        const button = this.dataTable.querySelector(COLLAPSE_SELECTOR);
        if (button) {
            // nothing to expand or collapse
            button.classList.toggle("d-none", !this.groups.length);
            button.textContent = this.isAllCollapsed ? "Expand all" : "Collapse all";
        }
    }

    _onClick = (ev) => {
        if (ev.target.closest(COLLAPSE_SELECTOR)) {
            this.toggleAll();
            return;
        }
        const header = ev.target.closest(GROUP_HEADER_SELECTOR);
        if (header && !ev.target.closest(INTERACTIVE_SELECTOR)) {
            header.closest(GROUP_SELECTOR).classList.toggle(HIDDEN_GROUP_CLASS);
            this.updateCollapseButton();
        }
    };
}
