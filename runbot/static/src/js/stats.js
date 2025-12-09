class StatsSearchState {
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
    static localParams = ["display_aggregate", "mode", "nb_dataset", "visible_keys"];
    static numberParams = ["limit", "center_build_id", "nb_dataset"];

    options = {};
    params = null;
    fetchController = null;
    fetchDelay = 250;
    bundleId = null;
    _params = { ...this.constructor.defaultParams };

    constructor(options = {}) {
        this.options = options;
        this.params = new Proxy(this._params, {
            set: this.setParam.bind(this),
        });
        this.fetchDataDebounced = debounce(this.fetchData, this.fetchDelay);
        window.addEventListener("hashchange", this.loadFromHash.bind(this));
    }

    async setParam(obj, prop, newVal) {
        if (this.constructor.numberParams.includes(prop)) {
            const num = Number(newVal);
            newVal = num ? num : 0;
        }
        if (String(obj[prop]) !== String(newVal)) {
            console.debug("params#set", {prop, oldVal: obj[prop], newVal});
            obj[prop] = newVal;
            this.updateHash();
            if (!this.constructor.localParams.includes(prop)) {
                await this.fetchDataDebounced();
            }
            this.options.onParamChanged?.(prop, newVal);
        }
        return true;
    }

    loadFromHash(ev = undefined) {
        const { hash } = new URL(ev ? ev.newURL : window.location.href);
        const params = hash.slice(1);
        console.debug("loadFromHash", params);
        for (const [key, value] of new URLSearchParams(params).entries()) {
            this.params[key] = value;
        }
    }

    updateHash() {
        window.location.hash = new URLSearchParams(this.params).toString();
    }

    fetchData() {
        try {
            this.fetchController?.abort("Search parameters updated");
            this.fetchController = new AbortController();
            return fetchChartData({ bundleId: this.bundleId, signal: this.fetchController.signal });
        } catch (e) {
            if (e.name !== "AbortError") {
                throw e;
            }
        }
    }
}

const config = {
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
        onClick: onClickChart,
    },
};

const searchState = new StatsSearchState({
    async onParamChanged(param, val) {
        updateChart();
        updateFilterSelector(param, val);
    },
});

searchState.bundleId = document.getElementById("bundle_id").value;
searchState.params.trigger_id = document.getElementById("trigger_id_selector").value;

for (const select of [...document.querySelectorAll("select[id$='_selector']")]) {
    const filterName = select.id.replace("_selector", "");
    updateFilterSelector(filterName, searchState.params[filterName]);
    select.addEventListener("change", () => {
        searchState.params[filterName] = select.value;
    });
}

document.getElementById("backward_button").addEventListener("click", () => {
    searchState.params.center_build_id = Object.keys(config.result)[0];
});

document.getElementById("forward_button").addEventListener("click", () => {
    searchState.params.center_build_id = Object.keys(config.result).slice(-1)[0];
});

document.getElementById("fast_forward_button").addEventListener(() => {
    searchState.params.center_build_id = 0;
});

searchState.loadFromHash();

/**
 * Creates and returns a new debounced version of the passed function (func)
 * which will postpone its execution until after 'delay' milliseconds
 * have elapsed since the last time it was invoked. The debounced function
 * will return a Promise that will be resolved when the function (func)
 * has been fully executed.
 *
 * @template {Function} T the return type of the original function
 * @param {T} func the function to debounce
 * @param {number} delay how long should elapse before the function is called.
 * @returns {T & { cancel: () => void }} the debounced function
 */
function debounce(func, delay) {
    const funcName = func.name ? `${func.name} (debounce)` : "debounce";
    let handle;
    let lastArgs;
    return Object.assign(
        {
            /** @type {any} */
            [funcName](...args) {
                const { promise, resolve } = Promise.withResolvers();
                lastArgs = args;
                clearTimeout(handle);
                handle = setTimeout(async () => {
                    handle = null;
                    if (lastArgs) {
                        Promise.resolve(func.apply(this, lastArgs)).then(resolve);
                        lastArgs = null;
                    }
                }, delay);
                return promise;
            },
        }[funcName],
        {
            cancel() {
                clearTimeout(handle);
            },
        },
    );
}

function onClickChart(event, activeElements) {
    if (activeElements.length === 0) {
        return;
    }
    const build_id = config.data.builds[activeElements[0].index];
    if (event.native.shiftKey) {
        searchState.params.center_build_id = build_id;
    } else {
        window.open("/runbot/build/" + build_id);
    }
}

function updateFilterSelector(filterName, val) {
    const select = document.getElementById(`${filterName}_selector`);
    if (!select) {
        return;
    }
    console.debug("updateFilterSelector", { prop: filterName, oldVal: select.value, newVal: val });
    if (select.value !== String(val)) {
        select.value = val;
    }
}

function random_color(name) {
    const colors = ["#004acd", "#3658c3", "#4a66ba", "#5974b2", "#6581aa", "#6f8fa3", "#7a9c9d", "#85a899", "#91b596", "#a0c096", "#fdaf56", "#f89a59", "#f1865a", "#e87359", "#dc6158", "#ce5055", "#bf4150", "#ad344b", "#992a45", "#84243d"];
    let sum = 0;
    for (let i = 0; i < name.length; i++) {
        sum += name.charCodeAt(i);
    }
    sum = sum % colors.length;
    const color = colors[sum];
    return color;
}

function process_chart_data() {
    if (!config.result || Object.keys(config.result).length == 0) {
        config.data = {
            labels: [],
            datasets: [],
        };
        return;
    }

    const aggregate = document.getElementById("display_aggregate_selector").value;
    const builds = Object.keys(config.result);
    const newer_build_stats = config.result[builds.slice(-1)[0]];
    const older_build_stats = config.result[builds[0]];
    const keys = Object.keys(newer_build_stats);
    if (aggregate != "sum") {
        keys.splice(keys.indexOf("Aggregate Sum"), 1);
    }
    if (aggregate != "average") {
        keys.splice(keys.indexOf("Aggregate Average"), 1);
    }
    const mode = document.getElementById("mode_selector").value;

    const sort_values = {};
    for (const key of keys) {
        let sort_value = NaN;
        if (mode == "normal") {
            sort_value = newer_build_stats[key];
        } else if (mode == "alpha") {
            sort_value = key;
        } else if (mode == "change_count") {
            sort_value = 0;
            let previous = undefined;
            for (const build of builds) {
                const res = config.result[build];
                const value = res[key];
                if (previous !== undefined && value !== undefined && previous != value) {
                    sort_value += 1;
                }
                previous = value;
            }
        } else {
            if (mode == "difference") {
                let previous_value = 0;
                if (older_build_stats[key] !== undefined) {
                    previous_value = older_build_stats[key];
                }
                sort_value = Math.abs(newer_build_stats[key] - previous_value);
            }
        }
        sort_values[key] = sort_value;
    }
    keys.sort((m1, m2) => sort_values[m2] - sort_values[m1]);

    let visible_keys;
    if (searchState.params.visible_keys) {
        visible_keys = new Set(searchState.params.visible_keys.split("-"));
    } else {
        visible_keys = new Set(keys.slice(0, searchState.params.nb_dataset));
    }

    function display_value(key, build_stats) {
        if (build_stats[key] === undefined) {
            return NaN;
        }
        if (mode == "normal" || mode == "alpha") {
            return build_stats[key];
        }
        let previous_value = 0;
        if (older_build_stats[key] !== undefined) {
            previous_value = older_build_stats[key];
        }
        return build_stats[key] - previous_value;
    }

    config.data = {
        builds: builds,
        labels: builds.map((build) => config.dates[build]),
        datasets: keys.map(function (key) {
            return {
                label: key,
                data: builds.map((build) => display_value(key, config.result[build])),
                borderColor: random_color(key),
                backgroundColor: "rgba(0, 0, 0, 0)",
                lineTension: 0,
                hidden: !visible_keys.has(key),
            };
        }),
    };
}

async function fetchChartData({ bundleId, signal }) {
    const chart_spinner = document.getElementById("chart_spinner");
    chart_spinner.style.visibility = "visible";
    const params = {
        ...searchState.params,
        bundle_id: bundleId,
    };
    console.debug("fetchChartData", params);
    const response = await fetch("/runbot/stats/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params }),
        signal,
    });
    const { result } = await response.json();
    console.debug("fetchChartData", result);
    chart_spinner.style.visibility = "hidden";
    if (result) {
        config.result = result["stats"];
        config.dates = result["dates"];
        Object.values(config.result).forEach((v) => v["Aggregate Sum"] = Object.values(v).reduce((a, b) => a + b, 0));
        Object.values(config.result).forEach((v) => v["Aggregate Average"] = Object.values(v).reduce((a, b) => a + b, 0) / Object.values(v).length);
    }
}

function onClickLegendItem(data) {
    return () => {
        data.hidden = !data.hidden;
        console.debug("onClickLegendItem", data, window.statsChart.data.datasets);
        searchState.params.visible_keys = window.statsChart.data.datasets.filter((dataset) => !dataset.hidden).map((dataset) => dataset.label).join("-");
    };
}

function renderLegend() {
    const legendContainer = document.querySelector("#js-legend");
    const legend = document.createElement("ul");
    legend.classList.add("list-unstyled");
    const items = [];
    for (const data of window.statsChart.data.datasets) {
        const legendItem = document.createElement("li");
        legendItem.classList.add("chart-legend-item", "ps-1", "fw-bold", "text-truncate");
        legendItem.classList.toggle("disabled", data.hidden);
        legendItem.style.setProperty("--chart-legend-item-accent", data.borderColor);
        legendItem.title = data.label;
        legendItem.append(data.label);
        legendItem.addEventListener("click", onClickLegendItem(data));
        items.push(legendItem);
    }
    legend.append(...items);
    legendContainer.replaceChildren(legend);
}

function updateForm() {
    const display_forward = config.result && searchState.params.center_build_id != 0 && (searchState.params.center_build_id !== Object.keys(config.result).slice(-1)[0]);
    document.getElementById("forward_button").style.visibility = display_forward ? "visible" : "hidden";
    document.getElementById("fast_forward_button").style.visibility = display_forward ? "visible" : "hidden";
    const display_backward = config.result && (searchState.params.center_build_id !== Object.keys(config.result)[0]);
    document.getElementById("backward_button").style.visibility = display_backward ? "visible" : "hidden";
}

function updateChart() {
    updateForm();
    process_chart_data();
    if (!window.statsChart) {
        const ctx = document.getElementById("canvas").getContext("2d");
        window.statsChart = new Chart(ctx, config);
    } else {
        window.statsChart.update();
    }
    renderLegend();
}
