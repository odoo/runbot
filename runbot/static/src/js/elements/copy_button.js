import { BaseButton } from "./base_button";

class CopyButton extends BaseButton {
    connectedCallback() {
        super.connectedCallback();
        this.addEventListener("click", this._onCopy);
    }

    disconnectedCallback() {
        super.disconnectedCallback();
        this.removeEventListener("click", this._onCopy);
    }

    _onCopy() {
        if (!navigator.clipboard) {
            // eslint-disable-next-line no-console
            console.error("Clipboard not supported");
            return;
        }
        navigator.clipboard.writeText(this.dataset.copyText);
    }
}

customElements.define("copy-button", CopyButton);
