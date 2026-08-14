// @odoo-module ignore

class TableGroup {
    static selector = ".table-group";
    static groupSelector = ".table-group-divider";
    static groupHeaderSelector = "tr:has(> th)";
    static groupRowSelector = "tr:not(:has(> th))";
    static collapseGroupsSelector = "[data-toggle='table-group-collapse']";
    static hiddenGroupClass = "table-group-hidden";

    constructor(el) {
        this.el = el;
        this.expanded = !this.isAllCollapsed;
        for (const group of this.groups) {
            const header = this.groupHeader(group);
            header.querySelector("th").addEventListener("click", () => this.toggleGroup(group));
        }
        this.toggleCollapseText();
        this.collapseButton.addEventListener("click", () => this.toggleCollapse());
    }

    get groups() {
        return [...this.el.querySelectorAll(this.constructor.groupSelector)];
    }

    get collapseButton() {
        return this.el.querySelector(this.constructor.collapseGroupsSelector);
    }

    get isAllCollapsed() {
        return this.groups.every((group) => group.classList.contains(this.constructor.hiddenGroupClass));
    }

    groupHeader(group) {
        return group.querySelector(this.constructor.groupHeaderSelector);
    }

    toggleGroup(group, force) {
        if (force === undefined) {
            group.classList.toggle(this.constructor.hiddenGroupClass);
        } else {
            group.classList.toggle(this.constructor.hiddenGroupClass, force);
        }
    }

    toggleCollapse() {
        this.expanded = !this.expanded;
        for (const group of this.groups) {
            this.toggleGroup(group, !this.expanded);
        }
        this.toggleCollapseText();
    }

    toggleCollapseText() {
        this.collapseButton.textContent = this.expanded ? "Collapse all" : "Expand all";
    }
}

document.addEventListener("DOMContentLoaded", () => {
    for (const table of [...document.querySelectorAll(TableGroup.selector)]){
        new TableGroup(table);
    }
});
