// @odoo-module ignore

class TableFilter {
    static selector = ".table-filter";
    static filterRowSelector = "[data-toggle='filter-row']";

    constructor(el) {
        this.el = el;
        for (const filter of this.filters) {
            filter.addEventListener("change", () => this.onFilter());
        }
        this.onFilter();
    }

    get filters() {
        return [...this.el.querySelectorAll(this.constructor.filterRowSelector)];
    }

    get rows() {
        return [...this.el.querySelectorAll("tbody > tr:not(:has(th))")];
    }

    onFilter() {
        const filters = this.filters.map((filter) => {
            const [key, val] = filter.dataset.filter.split("==");
            return { checked: filter.checked, selector: `tr:has([data-${key}="${val}"])` };
        });
        for (const row of this.rows) {
            const isFilteredOut = filters.some(({ checked, selector }) =>
                !checked && row.matches(selector),
            );
            row.classList.toggle("d-none", isFilteredOut);
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    for (const table of [...document.querySelectorAll(TableFilter.selector)]){
        new TableFilter(table);
    }
});
