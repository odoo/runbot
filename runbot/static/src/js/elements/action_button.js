import { BaseButton } from "./base_button";

const CSRF_TOKEN_SELECTOR = "meta[name='csrf-token']";

// re-render the current page in place
async function refresh() {
    const response = await fetch(window.location.href);
    if (!response.ok || response.redirected) {
        window.location.reload();
        return;
    }
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    document.title = page.title;
    document.body.replaceWith(page.body);
}

/**
 * Posts to `data-url` along with the current `location`, then follows the url returned by the controller if
 * any, or refreshes the current page.
 */
class ActionButton extends BaseButton {
    connectedCallback() {
        super.connectedCallback();
        this.addEventListener("click", this._onAction);
    }

    disconnectedCallback() {
        super.disconnectedCallback();
        this.removeEventListener("click", this._onAction);
    }

    async _onAction() {
        this.setAttribute("disabled", "");
        try {
            const response = await fetch(this.dataset.url, {
                method: "POST",
                body: new URLSearchParams({
                    csrf_token: document.querySelector(CSRF_TOKEN_SELECTOR).content,
                    location: window.location.href,
                }),
            });
            // e.g. expired session redirecting to the login page
            if (response.redirected) {
                window.location.reload();
                return;
            }
            if (!response.ok) {
                console.error(`${response.status} ${response.statusText}`);
                this.removeAttribute("disabled");
                return;
            }
            const url = (await response.text()).trim();
            if (url) {
                window.location.assign(url);
            } else {
                await refresh();
            }
        } catch (error) {
            console.error(error);
            this.removeAttribute("disabled");
        }
    }
}

customElements.define("action-button", ActionButton);
