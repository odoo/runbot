import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

export class Runbot extends Interaction {
    static selector = "body";

    dynamicContent = {
        "[data-runbot]": {
            "t-on-click.prevent": this.onDataRunbotClick,
        },
        "[data-clipboard-copy]": {
            "t-on-click": this.onClipboardCopy,
        },
    };

    async onDataRunbotClick({ currentTarget }) {
        const { runbot: operation, runbotBuild } = currentTarget.dataset;
        if (!operation) {
            return;
        }
        let { href: url} = currentTarget;
        if (runbotBuild) {
            url = `/runbot/build/${runbotBuild}/${operation}`;
        }
        const response = await fetch(url, { method: "POST" });
        const responseText = await response.text();
        const { href: currentURL, pathname } = window.location;
        switch(operation) {
            case "rebuild":
                if (pathname.endsWith(`/build/${runbotBuild}`)) {
                    const redirectURL = new URL(currentURL);
                    redirectURL.pathname = redirectURL.pathname.replace(`/build/${runbotBuild}`, `/build/${responseText}`);
                    window.location.href = redirectURL.toString();
                }
                break;
            case "action":
                currentTarget.parentElement.innerText = responseText;
                break;
            default:
                window.location.reload();
        }
    }

    onClipboardCopy({ currentTarget }) {
        if (!navigator.clipboard) {
            // eslint-disable-next-line no-console
            console.warn("Clipboard not supported");
            return;
        }
        navigator.clipboard.writeText(currentTarget.dataset.clipboardCopy);
    }
}

registry.category("public.interactions").add("runbot", Runbot);
