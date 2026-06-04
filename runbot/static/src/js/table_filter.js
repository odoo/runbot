// @odoo-module ignore

class TableFilter {
    static selector = ".table-filter";
    static filterRowSelector = "[data-toggle='filter-row']";

    constructor(el) {
        this.el = el;
        for (const filter of this.filters) {
            this.onFilter(filter);
            filter.addEventListener("change", () => this.onFilter(filter));
        }
    }

    get filters() {
        return [...this.el.querySelectorAll(this.constructor.filterRowSelector)];
    }

    get rows() {
        return [...this.el.querySelectorAll("tbody > tr:not(:has(th))")];
    }

    onFilter(filter) {
        const [key, val] = filter.dataset.filter.split("==");
        const filteredRows = this.rows.filter((r) => r.matches([`tr:has([data-${key}^="${val}"])`]));
        for (const row of filteredRows) {
            row.classList.toggle("d-none", !filter.checked);
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    for (const table of [...document.querySelectorAll(TableFilter.selector)]){
        new TableFilter(table);
    }
});
