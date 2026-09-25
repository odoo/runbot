import { BaseButton } from "./base_button";

const HIDDEN_CLASS = "d-none";
const ICON_SELECTOR = ".fa";
const EXPANDED_ICON_CLASS = "fa-compress";
const COLLAPSED_ICON_CLASS = "fa-expand";
const EXPANDED_LABEL = "Collapse";
const COLLAPSED_LABEL = "Expand";

class ExpanderButton extends BaseButton {
    connectedCallback() {
        super.connectedCallback();
        this.expanded = !this.targets.some((target) => target.classList.contains(HIDDEN_CLASS));
        this.render();
        this.addEventListener("click", this._onToggle);
    }

    disconnectedCallback() {
        super.disconnectedCallback();
        this.removeEventListener("click", this._onToggle);
    }

    get targets() {
        return [...document.querySelectorAll(this.dataset.target)];
    }

    get icon() {
        return this.matches(ICON_SELECTOR) ? this : this.querySelector(ICON_SELECTOR);
    }

    render() {
        for (const target of this.targets) {
            target.classList.toggle(HIDDEN_CLASS, !this.expanded);
        }
        const label = `${this.expanded ? EXPANDED_LABEL : COLLAPSED_LABEL} ${this.dataset.label}`;
        this.setAttribute("aria-expanded", String(this.expanded));
        this.setAttribute("aria-label", label);
        this.title = label;
        this.icon.classList.toggle(EXPANDED_ICON_CLASS, this.expanded);
        this.icon.classList.toggle(COLLAPSED_ICON_CLASS, !this.expanded);
    }

    _onToggle() {
        this.expanded = !this.expanded;
        this.render();
    }
}

customElements.define("expander-button", ExpanderButton);
