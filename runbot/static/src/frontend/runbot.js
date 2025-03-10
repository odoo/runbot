import { registry } from '@web/core/registry';
import { Interaction } from '@web/public/interaction';


class Runbot extends Interaction {
    static selector = '.frontend';
    dynamicContent = {
        '[data-runbot]': {
            't-on-click.prevent': this.onClickDataRunbot,
        },
        '[data-clipboard-copy]': {
            't-on-click.prevent': this.onClickClipboardCopy
        }
    };

    /**
     * @param {Event} ev
     */
    async onClickDataRunbot({currentTarget: target}) {
        const {runbot: operation, runbotBuild} = target.dataset;
        if (!operation) {
            return;
        }
        let url = target.href;
        if (runbotBuild) {
            url = `/runbot/build/${runbotBuild}/${operation}`;
        }
        const response = await fetch(url, {
            method: 'POST',
        });
        if (operation == 'rebuild' && window.location.href.split('?')[0].endsWith(`/build/${runbotBuild}`)) {
            window.location.href = window.location.href.replace('/build/' + runbotBuild, '/build/' + await response.text());
        } else if (operation == 'action') {
            target.parentElement.innerText = await response.text();
        } else {
            window.location.reload();
        }
    }

    /**
     * @param {Event} ev
     */
    async onClickClipboardCopy({ currentTarget: target }) {
        if (!navigator.clipboard) {
            return;
        }
        navigator.clipboard.writeText(target.dataset.clipboardCopy);
    }
}

registry.category('public.interactions').add('runbot', Runbot);
