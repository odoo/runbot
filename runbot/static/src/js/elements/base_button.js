/**
 * A lightweight base class for accessible custom buttons in the Light DOM.
 * Automatically manages ARIA semantics, keyboard navigation (Enter/Space),
 * and focus states without relying on Shadow DOM or native wrapper elements.
 */
export class BaseButton extends HTMLElement {
    static get observedAttributes() {
        return ["disabled"];
    }

    connectedCallback() {
        // Set semantic role and focusability directly on the host tag
        if (!this.hasAttribute("role")) {
            this.setAttribute("role", "button");
        }
        if (!this.hasAttribute("tabindex") && !this.hasAttribute("disabled")) {
            this.setAttribute("tabindex", "0");
        }

        // Attach keyboard and click listeners
        this.addEventListener("keydown", this._onKeyDown);
        this.addEventListener("click", this._onClick);
    }

    disconnectedCallback() {
        this.removeEventListener("keydown", this._onKeyDown);
        this.removeEventListener("click", this._onClick);
    }

    attributeChangedCallback(name, oldValue, newValue) {
        if (name === "disabled") {
            const isDisabled = newValue !== null;
            this.setAttribute("aria-disabled", isDisabled ? "true" : "false");
            if (isDisabled) {
                this.removeAttribute("tabindex");
            } else {
                this.setAttribute("tabindex", "0");
            }
        }
    }

    _onKeyDown(event) {
        if (this.hasAttribute("disabled")) {
            return;
        }
        // Handle Space and Enter for keyboard accessibility
        if (event.key === " " || event.key === "Enter") {
            event.preventDefault();
            this.click();
        }
    }

    _onClick(event) {
        if (this.hasAttribute("disabled")) {
            event.stopImmediatePropagation();
            event.preventDefault();
        }
    }
}
