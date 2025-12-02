import { DelegatedInteractionLite } from "./delegated_interaction_lite";

export class Runbot extends DelegatedInteractionLite {
    dynamicContent = {
        "[data-runbot]": {
            "t-on-click.prevent": this.onDataRunbotClick,
        },
        "[data-clipboard-copy]": {
            "t-on-click": this.onClipboardCopy,
        },
    };

    async onDataRunbotClick({ delegatedTarget }) {
        const { runbot: operation, runbotBuild } = delegatedTarget.dataset;
        if (!operation) {
            return;
        }
        let { href: url} = delegatedTarget;
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
                delegatedTarget.parentElement.innerText = responseText;
                break;
            default:
                window.location.reload();
        }
    }

    onClipboardCopy({ delegatedTarget }) {
        if (!navigator.clipboard) {
            console.warn("Clipboard not supported");
            return;
        }
        navigator.clipboard.writeText(delegatedTarget.dataset.clipboardCopy);
    }
}
