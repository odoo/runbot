import { registry } from "@web/core/registry";
import { stepUtils } from "@web_tour/tour_utils";

registry.category("web_tour.tours").add("runbot_docker_main_flow", {
    steps: () => [
        stepUtils.showAppsMenuItem(),
        {
            content: "Open Runbot",
            trigger: ".o_app[data-menu-xmlid=\"runbot.runbot_menu_root\"]",
            run: "click",
        },
        {
            content: "Open the docker menu",
            trigger: "button[data-menu-xmlid=\"runbot.menu_dockerfile\"]",
            run: "click",
        },
        {
            content: "Open the dockerfiles list",
            trigger: "a[data-menu-xmlid=\"runbot.menu_dockerfiles\"]",
            run: "click",
        },
        {
            content: "Filter the dockerfiles on the tour fixture",
            trigger: ".o_searchview_input",
            run: "edit UiTourDockerfile",
        },
        {
            content: "Apply the dockerfile filter",
            trigger: ".o_searchview_input",
            run: "press Enter",
        },
        {
            content: "Open the dockerfile",
            trigger: ".o_data_row .o_data_cell:contains(\"UiTourDockerfile\")",
            run: "click",
        },
        {
            content: "The layers list highlights the raw layer instructions",
            trigger: `.o_field_widget[name="layer_ids"] .o_field_dockerfile.text-bg-success .o_runbot_dockerfile_keyword:contains("FROM")`,
        },
        {
            content: "The layers list highlights the file layer header",
            trigger: `.o_field_widget[name="layer_ids"] .o_field_dockerfile.text-bg-dark .o_runbot_dockerfile_file:contains("-> FILE /etc/ui_tour.conf")`,
        },
        {
            content: "Open the dockerfile tab",
            trigger: ".o_notebook_headers .nav-link:contains(\"Dockerfile\")",
            run: "click",
        },
        {
            content: "The dockerfile highlights the instruction options",
            trigger: `.o_field_widget[name="dockerfile"] .o_runbot_dockerfile_line .o_runbot_dockerfile_property:contains("--from")`,
        },
        {
            content: "The dockerfile highlights the shell syntax of RUN instructions",
            trigger: `.o_field_widget[name="dockerfile"]:has(.o_runbot_dockerfile_flag:contains("--no-install-recommends")):has(.o_runbot_dockerfile_shell-operator:contains("&&")):has(.o_runbot_dockerfile_key:contains("DEBIAN_FRONTEND="))`,
        },
    ],
});
