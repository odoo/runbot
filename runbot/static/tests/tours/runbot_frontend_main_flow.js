import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("runbot_frontend_main_flow", {
    steps: () => [
        {
            content: "Open the frontend tour project from the main Runbot page",
            trigger: `#top_menu .nav-link:contains("Runbot UI Tour Project")`,
            run: "click",
            expectUnloadPage: true,
        },
        {
            content: "The project frontend is selected",
            trigger: `.active_project:contains("Runbot UI Tour Project")`,
        },
        {
            content: "Search for the frontend tour bundle",
            trigger: `form[role="search"] input[name="search"]`,
            run: "edit ui-tour-active",
        },
        {
            content: "Submit the bundle search",
            trigger: `form[role="search"] button[type="submit"]`,
            run: "click",
            expectUnloadPage: true,
        },
        {
            content: "The bundles page renders the fixture batch",
            trigger: `.bundle_row:has(a[title="View Bundle ui-tour-active"]) .slot_name:contains("Frontend tour trigger")`,
        },
        {
            content: "Copy the bundle name",
            trigger: `.bundle_row:has(a[title="View Bundle ui-tour-active"]) button[data-copy-text="ui-tour-active"]`,
            async run() {
                const expected = "ui-tour-active";
                const origWriteText = browser.navigator.clipboard.writeText;
                browser.navigator.clipboard.writeText = (text) => {
                    let success = false;
                    if (text === expected) {
                        success = true;
                    }
                    browser.navigator.clipboard.writeText = origWriteText;
                    if (!success) {
                        throw new Error(`Clipboard mock received "${text}" but expected "${expected}"`);
                    }
                }
                await this.anchor.click();
            },
        },
        {
            content: "Open the accessible lazy build options",
            trigger: `.bundle_row:has(a[title="View Bundle ui-tour-active"]) build-options-dropdown[role="button"][tabindex="0"]`,
            run: "click",
        },
        {
            content: "The build options are rendered lazily",
            trigger: `.bundle_row:has(a[title="View Bundle ui-tour-active"]) build-options-dropdown + .dropdown-menu .dropdown-item:contains("Rebuild"):not(:visible)`,
        },
        {
            content: "Open the bundle from the bundles page",
            trigger: `a[title="View Bundle ui-tour-active"]`,
            run: "click",
            expectUnloadPage: true,
        },
        {
            content: "The bundle page renders its batch",
            trigger: `.batch_row .slot_name:contains("Frontend tour trigger")`,
        },
        {
            content: "Open the batch from the bundle page",
            trigger: `.batch_row .batch_tile > .card > a`,
            run: "click",
            expectUnloadPage: true,
        },
        {
            content: "The batch page renders the bundle and build",
            trigger: `table:has(td:contains("Builds")) .slot_name:contains("Frontend tour trigger")`,
        },
        {
            content: "Open the build from the batch page",
            trigger: `table:has(td:contains("Builds")) .slot_name:contains("Frontend tour trigger")`,
            run: "click",
            expectUnloadPage: true,
        },
        {
            content: "The build page preserves the bundle, batch, and build context",
            trigger: `.breadcrumb:has(a:contains("ui-tour-active")):has(a:contains("Frontend tour trigger")):has(a:contains("Frontend UI tour build"))`,
        },
        {
            content: "Hide successful build rows",
            trigger: `button[data-toggle="hide-success"][aria-expanded="true"]`,
            run: "click",
        },
        {
            content: "The build page records the successful-row preference",
            trigger: `html.hide-success button[data-toggle="hide-success"][aria-expanded="false"]`,
        },
    ],
});
