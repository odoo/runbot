import { BaseButton } from "./base_button";

const MAX_VISIBLE_PROJECTS = 10;
const NO_MATCH = -1;

const INPUT_SELECTOR = "[data-project-filter='input']";
const ITEM_SELECTOR = "[data-project-filter='item']";
const EMPTY_SELECTOR = "[data-project-filter='empty']";
const HINT_SELECTOR = "[data-project-filter='hint']";

function matchRank(query, text) {
    query = query.toLowerCase();
    text = text.toLowerCase();
    if (text === query) {
        return 0;
    }
    if (text.startsWith(query)) {
        return 1;
    }
    if (text.includes(query)) {
        return 2;
    }
    let queryIndex = 0;
    for (let i = 0; i < text.length && queryIndex < query.length; i++) {
        if (text[i] === query[queryIndex]) {
            queryIndex++;
        }
    }
    return queryIndex === query.length ? 3 : NO_MATCH;
}

class ProjectDropdown extends BaseButton {
    connectedCallback() {
        super.connectedCallback();

        const menu = this.dataset.targetMenu
            ? document.querySelector(this.dataset.targetMenu)
            : this.nextElementSibling;
        this.menu = menu?.classList.contains("dropdown-menu") ? menu : null;
        this.items = [...(this.menu?.querySelectorAll(ITEM_SELECTOR) || [])];
        this.input = this.menu?.querySelector(INPUT_SELECTOR);
        if (!this.input && !this.items.length) {
            throw new Error("project-dropdown needs a project list, a filter input, or both");
        }

        this.classList.add("dropdown-toggle");
        this.setAttribute("data-bs-toggle", "dropdown");
        this.setAttribute("aria-expanded", "false");

        if (!this.input) {
            return;
        }
        const prefix = (this.dataset.prefix || "").toLowerCase();
        this.matchText = new Map();
        for (const item of this.items) {
            const name = item.dataset.name || item.textContent.trim();
            if (name.toLowerCase().startsWith(prefix)) {
                this.matchText.set(item, name.slice(prefix.length));
            }
        }
        this.rowsStart = this.input.closest("li");
        this.empty = this.menu.querySelector(EMPTY_SELECTOR);
        this.hint = this.menu.querySelector(HINT_SELECTOR);

        this.input.addEventListener("input", () => this.filter());
        this.input.addEventListener("keydown", (ev) => this.onInputKeyDown(ev));
        this.menu.addEventListener("keydown", (ev) => this.onMenuKeyDown(ev));
        this.addEventListener("shown.bs.dropdown", () => this.reset());
        this.filter();
    }

    reset() {
        this.input.value = "";
        this.filter();
        this.input.focus();
    }

    toggleRow(row, shown) {
        row?.classList.toggle("d-none", !shown);
    }

    showHint(count) {
        if (!this.hint) {
            return;
        }
        this.toggleRow(this.hint, count > 0);
        if (count > 0) {
            this.hint.textContent = `+${count} more project${count > 1 ? "s" : ""}, refine your search`;
        }
    }

    reorderRows(items) {
        let anchor = this.rowsStart;
        for (const item of items) {
            const row = item.closest("li");
            anchor.after(row);
            anchor = row;
        }
    }

    filter() {
        const query = this.input.value.trim();
        const matches = [...this.matchText]
            .map(([item, text]) => [item, matchRank(query, text)])
            .filter(([, rank]) => rank !== NO_MATCH)
            .sort((a, b) => a[1] - b[1])
            .map(([item]) => item);
        this.visible = matches.slice(0, MAX_VISIBLE_PROJECTS);
        for (const item of this.items) {
            this.toggleRow(item.closest("li"), this.visible.includes(item));
        }
        this.reorderRows(this.visible);
        this.toggleRow(this.empty, !matches.length);
        this.showHint(matches.length - this.visible.length);
    }

    onInputKeyDown(event) {
        if (!this.visible.length || !["Enter", "ArrowDown", "ArrowUp"].includes(event.key)) {
            return;
        }
        event.preventDefault();
        if (event.key === "Enter") {
            this.visible[0].click();
        } else if (event.key === "ArrowDown") {
            this.visible[0].focus();
        } else {
            this.visible.at(-1).focus();
        }
    }

    onMenuKeyDown(event) {
        const leavesTop = event.key === "ArrowUp" && event.target === this.visible[0];
        const leavesBottom = event.key === "ArrowDown" && event.target === this.visible.at(-1);
        if (!leavesTop && !leavesBottom) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        this.input.focus();
    }
}

customElements.define("project-dropdown", ProjectDropdown);
