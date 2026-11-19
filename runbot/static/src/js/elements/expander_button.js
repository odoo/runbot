import { BaseButton } from "./base_button";

const DEFAULT_COLLAPSED_CLASS = "d-none";
const ICON_SELECTOR = ".fa-expand, .fa-compress";
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
        // looked up once, as toggling its classes changes what the selector matches
        const icon = this.icon;
        icon?.classList.toggle(EXPANDED_ICON_CLASS, this.expanded);
        icon?.classList.toggle(COLLAPSED_ICON_CLASS, !this.expanded);
    }

    _onToggle() {
        this.expanded = !this.expanded;
        this.render();
        // keep the state in the url, e.g. to restore it on reload
        if (this.dataset.param) {
            const url = new URL(window.location);
            if (this.expanded) {
                url.searchParams.set(this.dataset.param, "1");
            } else {
                url.searchParams.delete(this.dataset.param);
            }
            window.history.replaceState(window.history.state, "", url);
        }
    }
}

customElements.define("expander-button", ExpanderButton);
