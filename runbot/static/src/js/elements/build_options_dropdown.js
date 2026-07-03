import { extractDataset } from "../utils/dataset";
import { render } from "../utils/templating";
import { BaseButton } from "./base_button";

const LAZY_INTERACTION_EVENTS = ["mouseenter", "focus", "touchstart", "click"];

class BuildOptionsDropdown extends BaseButton {
    connectedCallback() {
        super.connectedCallback();
        this.template = document.getElementById("build-options-dropdown-menu");
        this.data = extractDataset(this);
        this.classList.add("dropdown-toggle");
        this.setAttribute("data-bs-toggle", "dropdown");
        this.setAttribute("aria-expanded", "false");
        const lazyBuildMenuHandler = () => {
            if (this.nextElementSibling?.classList.contains("dropdown-menu")) {
                return;
            }
            const renderedMenu = render(this.template.content.cloneNode(true), this.data);
            this.after(renderedMenu);
            for (const eventType of LAZY_INTERACTION_EVENTS) {
                this.removeEventListener(eventType, lazyBuildMenuHandler);
            }
        } ;
        for (const eventType of LAZY_INTERACTION_EVENTS) {
            this.addEventListener(eventType, lazyBuildMenuHandler, { once: true });
        }
    }
}

customElements.define("build-options-dropdown", BuildOptionsDropdown);
