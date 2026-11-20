import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("runbot_stats_main_flow", {
    steps: () => [
        {
            content: "The stats are sorted on their latest value",
            trigger: `#js-legend li:first-child:contains("account") + li:contains("sale_management") + li:contains("base")`,
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
            trigger: "#forward_button",
        },
        {
            content: "The chart is centered on the oldest build",
            trigger: "#backward_button:not(:visible)",
        },
        {
            content: "Go back to the latest builds",
            trigger: "#fast_forward_button",
            run: "click",
        },
        {
            content: "The chart shows the latest builds",
            trigger: "#fast_forward_button:not(:visible)",
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
    ],
});
