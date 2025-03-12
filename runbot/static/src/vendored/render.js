/** @odoo-module alias=@web/core/utils/render default=false **/

import { App, blockDom, Component } from "@runbot/owl";
import { getTemplate } from "@web/core/templates";


export function renderToFragment(template, context = {}) {
    const frag = document.createDocumentFragment();
    for (const el of [...render(template, context).children]) {
        frag.appendChild(el);
    }
    return frag;
}

let app;
Object.defineProperty(renderToFragment, "app", {
    get: () => {
        if (!app) {
            app = new App(Component, {
                name: "renderToFragment",
                getTemplate,
            });
        }
        return app;
    },
});

function render(template, context = {}) {
    const app = renderToFragment.app;
    const templateFn = app.getTemplate(template);
    const bdom = templateFn(context, {});
    const div = document.createElement("div");
    blockDom.mount(bdom, div);
    return div;
}
