import { registry } from "@web/core/registry";
import { stepUtils } from "@web_tour/tour_utils";

registry.category("web_tour.tours").add("runbot_error_main_flow", {
    steps: () => [
        stepUtils.showAppsMenuItem(),
        {
            content: "Open Runbot",
            trigger: ".o_app[data-menu-xmlid=\"runbot.runbot_menu_root\"]",
            run: "click",
        },
        {
            content: "Open error management",
            trigger: "button[data-menu-xmlid=\"runbot.runbot_menu_manage_errors\"]",
            run: "click",
        },
        {
            content: "Open the errors list",
            trigger: "a[data-menu-xmlid=\"runbot.runbot_menu_build_error_tree\"]",
            run: "click",
        },
        {
            content: "Errors list is rendered",
            trigger: ".o_content .o_list_renderer",
        },
        {
            content: "Filter the errors on the tour fixture",
            trigger: ".o_searchview_input",
            run: "edit UI tour error",
        },
        {
            content: "Apply the error filter",
            trigger: ".o_searchview_input",
            run: "press Enter",
        },
        {
            content: "The error list renders the compact pull request URL",
            trigger: ".o_data_row:has(.o_data_cell:contains(\"UI tour error\")) "
                + ".o_field_widget[name=\"fixing_pr_url\"] a:contains(\"odoo/runbot#12345\")",
        },
        {
            content: "The error list renders the history graph",
            trigger: ".o_data_row:has(.o_data_cell:contains(\"UI tour error\")) "
                + ".o_field_widget[name=\"history_data\"] canvas",
        },
        {
            content: "Open the error",
            trigger: ".o_data_row:has(.o_data_cell:contains(\"UI tour error\")) "
                + ".o_data_cell:contains(\"UI tour error\")",
            run: "click",
        },
        {
            content: "The compact pull request URL is also rendered on the form",
            trigger: ".o_form_view .o_field_widget[name=\"fixing_pr_url\"] "
                + "a:contains(\"odoo/runbot#12345\")",
        },
        {
            content: "The first seen date links to the Runbot build",
            trigger: ".o_form_view .o_field_widget[name=\"first_seen_date\"] "
                + "a[href^=\"/runbot/build/\"]",
        },
        {
            content: "Hover a history cell",
            trigger: ".o_form_view .o_field_widget[name=\"history_data\"] canvas",
            run() {
                const canvas = this.anchor;
                const rect = canvas.getBoundingClientRect();
                canvas.dispatchEvent(new MouseEvent("mousemove", {
                    bubbles: true,
                    clientX: rect.left + canvas.width - 7,
                    clientY: rect.top + 7,
                }));
            },
        },
        {
            content: "The history graph displays details for the hovered cell",
            trigger: ".history-graph-tooltip:contains(\"Version: master\")",
        },
        {
            content: "Open the qualifiers tab",
            trigger: ".o_notebook_headers .nav-link:contains(\"Qualifiers\")",
            run: "click",
        },
        {
            content: "The JSON field colorizes qualifier keys",
            trigger: ".o_field_widget.o_field_runbotjsonb[name=\"common_qualifiers\"] "
                + ".o_runbot_json_key:contains(\"test_class\")",
        },
        {
            content: "Update the multiline test tags",
            trigger: ".o_field_widget[name=\"test_tags\"] textarea",
            async run(helpers) {
                await helpers.edit("/runbot:new_failure\n/runbot:shared_context");
            },
        },
        ...stepUtils.saveForm(),
        {
            content: "The chatter renders removed lines as a diff",
            trigger: ".o-mail-Message-tracking .code_diff .code.removed:contains(\"old_failure\")",
        },
        {
            content: "The chatter renders added lines as a diff",
            trigger: ".o-mail-Message-tracking .code_diff .code.added:contains(\"new_failure\")",
        },
    ],
});
