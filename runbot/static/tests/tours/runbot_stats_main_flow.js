import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("runbot_stats_main_flow", {
    steps: () => [
        {
            content: "The stats are loaded",
            trigger: "#chart_spinner:not(:visible)",
        },
        {
            content: "Show the module loading queries",
            trigger: "#key_category_selector",
            run: "select module_loading_queries",
        },
        {
            content: "The stats are sorted on their latest value",
            trigger: `#js-legend li:first-child:contains("account") + li:contains("sale_management") + li:contains("base")`,
        },
        {
            content: "Sort the stats by name",
            trigger: "#mode_selector",
            run: "select alpha",
        },
        {
            content: "The stats are sorted by name",
            trigger: `#js-legend li:first-child:contains("account") + li:contains("base") + li:contains("sale_management")`,
        },
        {
            content: "Show the sum of the stats",
            trigger: "#display_aggregate_selector",
            run: "select sum",
        },
        {
            content: "The sum is shown",
            trigger: `#js-legend li:not(.disabled):contains("Aggregate Sum")`,
        },
        {
            content: "Center the chart on the previous builds",
            trigger: "#backward_button",
            run: "click",
        },
        {
            content: "The chart is centered, with newer builds",
            trigger: "#forward_button:enabled",
        },
        {
            content: "The chart is centered on the oldest build",
            trigger: "#backward_button:disabled",
        },
        {
            content: "Go back to the latest builds",
            trigger: "#fast_forward_button",
            run: "click",
        },
        {
            content: "The chart shows the latest builds",
            trigger: "#fast_forward_button:disabled",
        },
        {
            content: "Switch to another category",
            trigger: "#key_category_selector",
            run: "select js_test_memory_total",
        },
        {
            content: "The stats of the category are shown",
            trigger: `#js-legend li:not(.disabled):contains("memory_stat")`,
        },
        {
            content: "Hide all the stats",
            trigger: "#nb_dataset_selector",
            run: "select 0",
        },
        {
            content: "The stat is hidden",
            trigger: `#js-legend li.disabled:contains("memory_stat")`,
        },
        {
            content: "Show a stat from the legend",
            trigger: `#js-legend li:contains("memory_stat")`,
            run: "click",
        },
        {
            content: "The stat is shown in a custom selection",
            trigger: `#js-legend li:not(.disabled):contains("memory_stat")`,
        },
        {
            content: "The display is set to custom",
            trigger: `#nb_dataset_selector:has(option:checked[value="-1"])`,
        },
        {
            content: "The steps of the other triggers are disabled",
            trigger: `#key_step_selector:has(option:disabled[value="ui_tour_other_stats"])`,
        },
        {
            content: "Filter the stats on a step",
            trigger: "#key_step_selector",
            run: "select ui_tour_stats",
        },
        {
            content: "The stats of the step are loaded",
            trigger: "#chart_spinner:not(:visible)",
        },
        {
            content: "The stats of the step are shown",
            trigger: `#js-legend li:not(.disabled):contains("memory_stat")`,
        },
        {
            content: "Switch to another trigger",
            trigger: "#trigger_id_selector",
            run: "selectByLabel Stats tour other trigger",
        },
        {
            content: "The category switches to one of the trigger",
            trigger: `#key_category_selector:has(option:checked[value="js_test_memory_used"])`,
        },
        {
            content: "The step is reset, the trigger not having it",
            trigger: `#key_step_selector:has(option:checked[value=""]):has(option:disabled[value="ui_tour_stats"])`,
        },
        {
            content: "The stats of the trigger are shown",
            trigger: `#js-legend li:not(.disabled):contains("used_stat")`,
        },
    ],
});
