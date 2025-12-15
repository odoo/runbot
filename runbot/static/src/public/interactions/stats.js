import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { StatsChart } from "../components/stats_chart";
import { StatsChartLegend } from "../components/stats_chart_legend";

const LABEL_AGGREGATE_SUM = "Aggregate Sum";
const LABEL_AGGREGATE_AVERAGE = "Aggregate Average";

function generateColor(name) {
    const colors = ["#004acd", "#3658c3", "#4a66ba", "#5974b2", "#6581aa", "#6f8fa3", "#7a9c9d", "#85a899", "#91b596", "#a0c096", "#fdaf56", "#f89a59", "#f1865a", "#e87359", "#dc6158", "#ce5055", "#bf4150", "#ad344b", "#992a45", "#84243d"];
    let sum = 0;
    for (let i = 0; i < name.length; i++) {
        sum += name.charCodeAt(i);
    }
    sum = sum % colors.length;
    return colors[sum];
}

export class Stats extends Interaction {
    static selector = ".runbot-stats";
    static defaultParams = {
        key_step: "",
        limit: 25,
        center_build_id: 0,
        key_category: "module_loading_queries",
        mode: "normal",
        nb_dataset: 20,
        display_aggregate: "none",
        visible_keys: "",
    };
    static baseChartConfig = {
        type: "line",
        options: {
            animation: {
                duration: 0,
            },
            plugins: {
                legend: {
                    display: false,
                },
            },
            responsive: true,
            tooltips: {
                mode: "point",
            },
            scales: {
                x: {
                    display: true,
                    scaleLabel: {
                        display: true,
                        labelString: "Builds",
                    },
                },
                y: {
                    display: true,
                    scaleLabel: {
                        display: true,
                        labelString: "Value",
                    },
                },
            },
        },
    };
    static localParams = ["display_aggregate", "mode", "nb_dataset", "visible_keys"];
    static numberParams = ["limit", "center_build_id", "nb_dataset"];

    params = null;
    fetchController = null;
    fetchDelay = 250;
    loader = reactive({ isLoading: false });
    data = reactive({ dates: {}, stats: {} }, () => this.computeChartData());
    chartData = reactive({ labels: [], datasets: [] });
    triggersData = {};
    _params = { ...this.constructor.defaultParams };

    dynamicContent = {
        "_window": {
            "t-on-hashchange": this.loadFromHash,
        },
        "#backward_button": {
            "t-on-click": this.onClickBackward,
            "t-att-disabled": () => !this.hasBackwardBuilds,
        },
        "#forward_button": {
            "t-on-click": this.onClickForward,
            "t-att-disabled": () => !this.hasForwardBuilds,
        },
        "#fast_forward_button": {
            "t-on-click": this.onClickFastForward,
            "t-att-disabled": () => !this.hasForwardBuilds,
        },
        "select[id$='_selector']": {
            "t-on-change": this.onChangeFilterSelector,
        },
        "select[id$='_selector'] option": {
            "t-att-selected": (el) => {
                const filterName = el.closest("select").id.replace("_selector", "");
                return String(el.value) === String(this.params[filterName]) ? "selected" : undefined;
            },
        },
        "select#trigger_id_selector option": {
            "t-att-class": (el) => {
                const categories = Object.keys(this.triggersData.relations[el.value] || {});
                return { "text-secondary": !categories.find((category) => category === this.params.key_category) };
            },
        },
        "select#key_category_selector option": {
            "t-att-class": (el) => {
                const trigger = this.triggersData.relations[String(this.params.trigger_id)] || {};
                return { "text-secondary": !Object.keys(trigger).includes(el.value) };
            },
        },
        "select#key_step_selector option": {
            "t-att-class": (el) => {
                const trigger = this.triggersData.relations[String(this.params.trigger_id)] || {};
                const steps = trigger[this.params.key_category] || [];
                return { "text-secondary": !steps.includes(el.value) };
            },
        },
        "#js-chart": {
            "t-component": () => [StatsChart, { config: this.chartConfig, data: this.chartData, loader: this.loader }],
        },
        "#js-legend": {
            "t-component": () => [StatsChartLegend, { data: this.chartData, onClickItem: this.onClickLegendItem }],
        },
    };

    setup() {
        this.params = new Proxy(this._params, {
            set: this.setParam.bind(this),
        });
        this.loadFromHash();
        Object.assign(this.triggersData, { ...JSON.parse(document.getElementById("triggers_data").text) });
        this.params.trigger_id = this.triggersData.trigger_id;
    }

    start() {
        this.fetchData();
    }

    get chartConfig() {
        return {
            ...this.constructor.baseChartConfig,
            options: {
                ...this.constructor.baseChartConfig.options,
                onClick: this.onClickChart.bind(this),
            },
        };
    }

    get builds() {
        return Object.keys(this.data.stats);
    }

    computeChartData() {
        if (!Object.keys(this.data.dates).length || !Object.keys(this.data.stats).length) {
            Object.assign(this.chartData, { labels: [], datasets: [] });
        }
        const { display_aggregate, mode } = this.params;
        const newer_build_stats = this.data.stats[this.builds.slice(-1)[0]];
        const older_build_stats = this.data.stats[this.builds[0]];
        const keys = Object.keys(newer_build_stats);
        if (display_aggregate !== "sum") {
            keys.splice(keys.indexOf(LABEL_AGGREGATE_SUM), 1);
        }
        if (display_aggregate !== "average") {
            keys.splice(keys.indexOf(LABEL_AGGREGATE_AVERAGE), 1);
        }
        const sort_values = {};
        for (const key of keys) {
            let sort_value = NaN;
            if (mode === "normal") {
                sort_value = newer_build_stats[key];
            } else if (mode === "alpha") {
                sort_value = key;
            } else if (mode === "change_count") {
                sort_value = 0;
                let previous = undefined;
                for (const res of Object.values(this.data.stats)) {
                    const value = res[key];
                    if (previous !== undefined && value !== undefined && previous != value) {
                        sort_value += 1;
                    }
                    previous = value;
                }
            } else if (mode === "difference") {
                let previous_value = 0;
                if (older_build_stats[key] !== undefined) {
                    previous_value = older_build_stats[key];
                }
                sort_value = Math.abs(newer_build_stats[key] - previous_value);
            }
            sort_values[key] = sort_value;
        }
        keys.sort((m1, m2) => sort_values[m2] - sort_values[m1]);

        let visible_keys;
        if (this.params.visible_keys) {
            visible_keys = new Set(this.params.visible_keys.split("-"));
        } else {
            visible_keys = new Set(keys.slice(0, this.params.nb_dataset));
        }

        function display_value(key, build_stats) {
            if (build_stats[key] === undefined) {
                return NaN;
            }
            if (mode === "normal" || mode === "alpha") {
                return build_stats[key];
            }
            let previous_value = 0;
            if (older_build_stats[key] !== undefined) {
                previous_value = older_build_stats[key];
            }
            return build_stats[key] - previous_value;
        }

        Object.assign(this.chartData, {
            labels: this.builds.map((build) => this.data.dates[build]),
            datasets: keys.map((key) => ({
                label: key,
                data: this.builds.map((build) => display_value(key, this.data.stats[build])),
                borderColor: generateColor(key),
                backgroundColor: "rgba(0, 0, 0, 0)",
                lineTension: 0,
                hidden: !visible_keys.has(key),
            })),
        });
    }

    get hasForwardBuilds() {
        const { center_build_id } = this.params;
        const { stats } = this.data;
        return stats && center_build_id != 0 && center_build_id !== Object.keys(stats).slice(-1)[0];
    }

    get hasBackwardBuilds() {
        const { stats } = this.data;
        return stats && (this.params.center_build_id !== Object.keys(stats)[0]);
    }

    async setParam(obj, prop, newVal) {
        if (this.constructor.numberParams.includes(prop)) {
            const num = Number(newVal);
            newVal = num ? num : 0;
        }
        if (String(obj[prop]) !== String(newVal)) {
            obj[prop] = newVal;
            this.updateHash();
            if (this.isReady && !this.isDestroyed) {
                if (!this.constructor.localParams.includes(prop)) {
                    await this.debounced(this.fetchData, this.fetchDelay)();
                }
                this.updateContent();
            }
        }
        return true;
    }

    loadFromHash(ev = undefined) {
        const { hash } = new URL(ev ? ev.newURL : window.location.href);
        const params = hash.slice(1);
        for (const [key, value] of new URLSearchParams(params).entries()) {
            this.params[key] = value;
        }
    }

    updateHash() {
        window.location.hash = new URLSearchParams(this.params).toString();
    }

    async fetchData() {
        this.fetchController?.abort("Search parameters updated");
        this.loader.isLoading = true;
        const params = {
            ...this.params,
            bundle_id: this.triggersData.bundle_id,
        };
        this.fetchController = new AbortController();
        try {
            const response = await fetch("/runbot/stats/", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ params }),
                signal: this.fetchController.signal,
            });
            const { result, error } = await response.json();
            if (error) {
                throw new Error(error.message);
            }
            const { stats, dates } = result;
            for (const val of Object.values(stats)) {
                val[LABEL_AGGREGATE_SUM] = Object.values(val).reduce((a, b) => a + b, 0);
                val[LABEL_AGGREGATE_AVERAGE] = Object.values(val).reduce((a, b) => a + b, 0) / Object.values(val).length;
            }
            Object.assign(this.data, {
                stats,
                dates,
            });
            this.loader.isLoading = false;
        } catch (e) {
            if (e.name !== "AbortError") {
                this.loader.isLoading = false;
                throw e;
            }
        }
    }

    onClickLegendItem(data) {
        return () => {
            data.hidden = !data.hidden;
            this.params.visible_keys = this.chart.data.datasets.filter((dataset) => !dataset.hidden).map((dataset) => dataset.label).join("-");
        };
    }

    onClickChart(event, [activeElement]) {
        if (!activeElement) {
            return;
        }
        const buildId = this.builds[activeElement.index];
        if (event.native.shiftKey) {
            this.params.center_build_id = buildId;
        } else {
            window.open(`/runbot/build/${buildId}`);
        }
    }

    onClickBackward() {
        this.params.center_build_id = this.builds[0];
    }

    onClickForward() {
        this.params.center_build_id = this.builds.slice(-1)[0];
    }

    onClickFastForward() {
        this.params.center_build_id = 0;
    }

    onChangeFilterSelector({ currentTarget }) {
        const filterName = currentTarget.id.replace("_selector", "");
        this.params[filterName] = currentTarget.value;
    }
}

registry.category("public.interactions").add("stats", Stats);
