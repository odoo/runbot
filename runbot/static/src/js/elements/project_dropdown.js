import { BaseButton } from "./base_button";

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
        if (!this.input) {
            return;
        }

        this.input.addEventListener("input", () => this.onFilter());
        this.input.addEventListener("click", (ev) => ev.stopPropagation());
        this.addEventListener("shown.bs.dropdown", () => this.reset());
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
        let visibleCount = 0;
        for (const item of this.items) {
            const visible = !query || fuzzyMatch(query, item.dataset.name || item.textContent);
            item.closest("li").classList.toggle("d-none", !visible);
            visibleCount += visible ? 1 : 0;
        }
        this.empty?.classList.toggle("d-none", visibleCount > 0);
    }
}

customElements.define("project-dropdown", ProjectDropdown);
