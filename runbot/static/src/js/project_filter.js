// @odoo-module ignore

function fuzzyMatch(query, text) {
    query = query.toLowerCase();
    text = text.toLowerCase();
    let queryIndex = 0;
    for (let i = 0; i < text.length && queryIndex < query.length; i++) {
        if (text[i] === query[queryIndex]) {
            queryIndex++;
        }
    }
    return queryIndex === query.length;
}

class ProjectFilter {
    static selector = ".js_project_filter";
    static inputSelector = ".js_project_filter_input";
    static itemSelector = ".js_project_filter_item";
    static emptySelector = ".js_project_filter_empty";

    constructor(el) {
        this.el = el;
        this.input = el.querySelector(this.constructor.inputSelector);
        this.empty = el.querySelector(this.constructor.emptySelector);
        if (!this.input) {
            return;
        }
        this.input.addEventListener("input", () => this.onFilter());
        this.input.addEventListener("click", (ev) => ev.stopPropagation());
        el.addEventListener("shown.bs.dropdown", () => this.reset());
    }

    get items() {
        return [...this.el.querySelectorAll(this.constructor.itemSelector)];
    }

    reset() {
        this.input.value = "";
        this.onFilter();
        this.input.focus();
    }

    onFilter() {
        const query = this.input.value.trim();
        let visibleCount = 0;
        for (const item of this.items) {
            const visible = !query || fuzzyMatch(query, item.dataset.name || item.textContent);
            item.closest("li").classList.toggle("d-none", !visible);
            visibleCount += visible ? 1 : 0;
        }
        if (this.empty) {
            this.empty.classList.toggle("d-none", visibleCount > 0);
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    for (const el of [...document.querySelectorAll(ProjectFilter.selector)]) {
        new ProjectFilter(el);
    }
});
