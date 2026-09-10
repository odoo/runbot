import { BaseButton } from "./base_button";

const MAX_VISIBLE_PROJECTS = 10;

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

class ProjectDropdown extends BaseButton {
    connectedCallback() {
        super.connectedCallback();
        this.classList.add("dropdown-toggle");
        this.setAttribute("data-bs-toggle", "dropdown");
        this.setAttribute("aria-expanded", "false");

        this.menu = this.nextElementSibling?.classList.contains("dropdown-menu")
            ? this.nextElementSibling
            : null;
        this.input = this.menu?.querySelector(".js_project_filter_input");
        this.empty = this.menu?.querySelector(".js_project_filter_empty");
        this.hint = this.menu?.querySelector(".js_project_filter_hint");
        if (!this.input) {
            return;
        }

        this.input.addEventListener("input", () => this.onFilter());
        this.input.addEventListener("click", (ev) => ev.stopPropagation());
        this.addEventListener("shown.bs.dropdown", () => this.reset());
        this.onFilter();
    }

    get items() {
        return [...this.menu.querySelectorAll(".js_project_filter_item")];
    }

    reset() {
        this.input.value = "";
        this.onFilter();
        this.input.focus();
    }

    onFilter() {
        const query = this.input.value.trim();
        const matches = this.items.filter(
            (item) => !query || fuzzyMatch(query, item.dataset.name || item.textContent)
        );
        const visible = new Set(matches.slice(0, MAX_VISIBLE_PROJECTS));
        for (const item of this.items) {
            item.closest("li").classList.toggle("d-none", !visible.has(item));
        }
        this.empty?.classList.toggle("d-none", matches.length > 0);
        const hiddenCount = matches.length - visible.size;
        if (this.hint) {
            this.hint.classList.toggle("d-none", hiddenCount <= 0);
            if (hiddenCount > 0) {
                this.hint.textContent = `+${hiddenCount} more project${hiddenCount > 1 ? "s" : ""}, refine your search`;
            }
        }
    }
}

customElements.define("project-dropdown", ProjectDropdown);
