import { DataTablePlugin } from "./plugin";

const FILTER_SELECTOR = "[data-toggle='filter-row']";

/**
 * Rows matching an unchecked `filter-row` input's `data-filter-rule` (`key==value`) are hidden.
 */
export class FilterPlugin extends DataTablePlugin {
    connect() {
        this.dataTable.addEventListener("change", this._onChange);
        this.filterRows();
    }

    disconnect() {
        this.dataTable.removeEventListener("change", this._onChange);
    }

    filterRows() {
        const selectors = [...this.dataTable.querySelectorAll(FILTER_SELECTOR)]
            .filter((filter) => !filter.checked)
            .map((filter) => {
                const [key, value] = filter.dataset.filterRule.split("==");
                return `tr:has([data-${key}="${value}"])`;
            });
        for (const row of this.dataTable.rows) {
            row.classList.toggle("d-none", selectors.some((selector) => row.matches(selector)));
        }
    }

    _onChange = (ev) => {
        if (ev.target.matches(FILTER_SELECTOR)) {
            this.filterRows();
        }
    };
}
