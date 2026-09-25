import { BaseButton } from "./base_button";

const DEFAULT_COLLAPSED_CLASS = "d-none";
const ICON_SELECTOR = ".fa";
const EXPANDED_ICON_CLASS = "fa-compress";
const COLLAPSED_ICON_CLASS = "fa-expand";
const EXPANDED_LABEL = "Collapse";
const COLLAPSED_LABEL = "Expand";

class ExpanderButton extends BaseButton {
    connectedCallback() {
        super.connectedCallback();
        this.expanded = !this.targets.some((target) => target.classList.contains(this.collapsedClass));
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

    // class applied to the targets while collapsed
    get collapsedClass() {
        return this.dataset.collapsedClass || DEFAULT_COLLAPSED_CLASS;
    }

    get icon() {
        return this.matches(ICON_SELECTOR) ? this : this.querySelector(ICON_SELECTOR);
    }

    render() {
        for (const target of this.targets) {
            target.classList.toggle(this.collapsedClass, !this.expanded);
        }
        this.setAttribute("aria-expanded", String(this.expanded));
        // without a label, the content is the accessible name
        if (this.dataset.label) {
            const label = `${this.expanded ? EXPANDED_LABEL : COLLAPSED_LABEL} ${this.dataset.label}`;
            this.setAttribute("aria-label", label);
            this.title = label;
        }
        this.icon?.classList.toggle(EXPANDED_ICON_CLASS, this.expanded);
        this.icon?.classList.toggle(COLLAPSED_ICON_CLASS, !this.expanded);
    }

    _onToggle() {
        this.expanded = !this.expanded;
        this.render();
    }
}

customElements.define("expander-button", ExpanderButton);
