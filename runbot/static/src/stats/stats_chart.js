/** @odoo-module **/

import { Component, useEffect, useRef, useState } from '@odoo/owl';

import { debounce, filterKeys, randomColor } from '@runbot/utils';
import { useConfig, onConfigChange } from '@runbot/stats/use_config';
import { Chart } from '@runbot/chartjs';
import { getBundleName } from '@runbot/stats/cache';


/**
 * @typedef StatsQueryResult
 *
 * @property {Number} id id of the build
 * @property {Object.<string, number>} values object with value name as key and value as value
 * @property {String} create_date creation date of the build
 * @property {Number} bundle_id bundle id of the build
 */
export class StatsChart extends Component {
    static template = 'runbot.StatsChart';
    static props = {
        bundle_id: { type: Number },
        trigger_id: { type: Number },
    }
    
    setup() {
        this._fetchStats = debounce(this._fetchStats.bind(this));
        this.config = useConfig();
        this.canvas = useRef('canvas');
        this.state = useState({
            loading: false,
            data: {},
        });
        this.chartConfig = useState({
            type: 'line',
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
                    mode: 'point',
                },
                scales: {
                    x: {
                        type: 'time',
                        display: true,
                        scaleLabel: {
                            display: true,
                            labelString: 'Builds',
                        },
                    },
                    y: {
                        display: true,
                        scaleLabel: {
                            display: true,
                            labelString: 'Value',
                        },
                    },
                },
                onClick: (event, activeElements) => {
                    const { native: { shiftKey }} = event;
                    if (activeElements.length === 0) {
                        return;
                    }
                    const { datasetIndex, index } = activeElements[0];
                    const queryResult = this.chartConfig.data.datasets[datasetIndex].data[index]._queryResult;
                    if (!queryResult) {
                        return console.error('Queryresult not present for datasetIndex and index ', datasetIndex, index);
                    }
                    const { id } = queryResult;
                    if (shiftKey) {
                        this.config.center_build_id = id;
                    } else {
                        window.open(`/runbot/build/stats/${id}`);
                    }
                }
            },
        });

        onConfigChange(() => this.fetchStats(), true);
        useEffect(() => {
            this.updateChart();
        }, () => [
            this.canvas, this.state.data,
            ...Object.values(filterKeys(this.config, ['mode', 'display_aggregate']))
        ]);
    }

    /**
     * Whether to display the next button
     */
    get shouldDisplayNext() {
        const builds = Object.keys(this.state.data);
        if (!builds.length) {
            return false;
        }
        return this.config.center_build_id !== '0' && this.config.center_build_id !== builds[builds.length - 1];
    }

    /**
     * Whether to display the previous button
     */
    get shouldDisplayPrevious() {
        const builds = Object.keys(this.state.data);
        if (!builds.length) {
            return false;
        }
        return this.config.center_build_id !== builds[0];
    }

    /**
     * Called before actually fetching stat, this triggers the spinner while waiting
     * on the debounced _fetchStat.
     */
    fetchStats() {
        this.state.loading = true;
        this._fetchStats(); // debounced
    }

    /**
     * Fetches data from the backend.
     */
    async _fetchStats() {
        const fetchData = {
            ...this.config,
            bundle_id: this.props.bundle_id,
            trigger_id: this.props.trigger_id,
        };
        const result = await fetch('/runbot/stats/', {
            body: JSON.stringify({params: fetchData}),
            method: 'POST',
            headers: {
                ['Content-Type']: 'application/json',
            },
        });
        this.state.data = (await result.json()).result;
        this.state.loading = false;
    }

    /**
     * Recompute the chart data according to current data and layout.
     */
    _computeChartData() {
        if (!this.state.data || Object.keys(this.state.data).length === 0) {
            this.chartConfig.data = {
                labels: [],
                datasets: [],
            };
            return;
        }
        const {
            display_aggregate: aggregate,
            mode,
        } = this.config;

        /** @type {StatsQueryResult[]} */
        const data = this.state.data;
        const bundles = new Set(data.map(({bundle_id}) => bundle_id));
        const hasMultiBundle = bundles.size > 1;
        /**
         * Gets the label (group by key) for the given queryResult
         *
         * @param {StatsQueryResult} queryResult
         * @param {String} valueName
         */
        const getLabel = (queryResult, valueName) => {
            // Q: Change to bundleName if not main bundle?
            if (hasMultiBundle) {
                return `${valueName} (${getBundleName(queryResult.bundle_id)})`;
            }
            return `${valueName}`;
        }
        // Group values by (valueName, bundle Id) we want to separate bundle ids.
        const datasets = Object.values(data.reduce((agg, queryResult) => {
            Object.entries(queryResult.values).forEach(([valueName, value]) => {
                const label = getLabel(queryResult, valueName);
                if (!(agg[label])) {
                    agg[label] = {
                        label,
                        data: [],
                        borderColor: randomColor(label),
                        backgroundColor: 'rgba(0, 0, 0, 0)',
                        lineTension: 0,
                        hidden: false,
                        _queryResult: queryResult,
                    }
                }
                agg[label].data.push({
                    x: queryResult.create_date,
                    y: value,
                    _queryResult: queryResult,
                });
            })
            return agg;
        }, {}));

        // Compute selected aggregate
        if (aggregate != 'none') {
            const queryResultsByBundle = data.reduce((agg, queryResult) => {
                if (!(agg[queryResult.bundle_id])) {
                    agg[queryResult.bundle_id] = [];
                }
                agg[[queryResult.bundle_id]].push(queryResult);
                return agg
            }, {});
            Object.values(queryResultsByBundle).forEach((queryResults) => {
                const newData = queryResults.map((qs) => {
                    return {
                        x: qs.create_date,
                        y: Object.values(qs.values).reduce((s, v) => s + v, 0),
                        _queryResult: qs,
                    }
                });
                let label = getLabel(datasets[0]._queryResult, 'Aggregate Sum');
                if (aggregate === 'average') {
                    label = getLabel(datasets[0]._queryResult, 'Aggregate Average');
                    newData.forEach(d => d.y /= Object.values(d._queryResult.values).length);
                }
                datasets.push({
                    label,
                    data: newData,
                    borderColor: randomColor(label),
                    backgroundColor: 'rgba(0, 0, 0, 0)',
                    lineTension: 0,
                    hidden: false,
                    _queryResult: datasets[0]._queryResult,
                });
            });
        }
        // Compute a sorting value for each dataset, sort
        // Also recompute data if mode requires it.
        datasets.forEach((dataset) => {
            const getSortValue = () => {
                if (mode === 'normal') {
                    return dataset.data[0].y;
                } else if (mode === 'alpha') {
                    return dataset.label;
                } else if (mode === 'change_count') {
                    return dataset.data.reduce((agg, {y}, dataIdx) => {
                        const previous = dataIdx === 0 ? undefined : dataset.data[dataIdx - 1].y;
                        if (previous !== undefined && y !== undefined && previous != y) {
                            agg += 1;
                        }
                        return agg;
                    }, 0);
                } else if (mode === 'difference') {
                    return Math.abs(
                        dataset.data[dataset.data.length - 1].y - dataset.data[0].y
                    );
                }
            }
            dataset._sortValue = getSortValue();
            if (mode === 'change_count' || mode === 'difference') {
                const firstValue = dataset.data[0].y;
                dataset.data = dataset.data.map(d => {
                    return {
                        ...d,
                        y: d.y - firstValue,
                    };
                });
            }
        });
        datasets.sort((ds1, ds2) => {
            if (mode === 'alpha') {
                return ds1._sortValue.localeCompare(ds2._sortValue);
            }
            return ds2._sortValue - ds1._sortValue;
        });
        this.chartConfig.data = {
            datasets,
        };
    }

    /**
     * Recomputes the visibility of the datasets according to config.
     */
    _computeVisibility() {
        let visibleKeys;
        if (this.config.nb_dataset !== -1) {
            visibleKeys = new Set(this.chartConfig.data.datasets.slice(0, this.config.nb_dataset).map(ds => ds.label));
        } else {
            visibleKeys = new Set(this.config.getVisibleKeys());
        }
        this.chartConfig.data.datasets.forEach(ds => ds.hidden = !visibleKeys.has(ds.label));
    }

    /**
     * Compute chart data and trigger an update on the chart.
     * If the canvas is not set, nothing happens.
     *
     * @param {Boolean} recompute whether to recompute the chart's dataset or not.
     */
    updateChart(recompute = true, computeVisibility = true) {
        if (!this.canvas || !this.canvas.el) {
            return
        }
        if (recompute) {
            this._computeChartData();
        }
        if (computeVisibility) {
            this._computeVisibility();
        }
        if (!this.chart) {
            this.chart = new Chart(this.canvas.el.getContext('2d'), this.chartConfig);
        } else {
            this.chart.update();
        }
    }

    /**
     * Pushes the visible keys from the current chart config.
     */
    _pushCurrentVisibleKeys() {
        this.config.pushVisibleKeys(
            this.chartConfig.data.datasets.filter(ds => !ds.hidden).map(ds => ds.label)
        );
    }
    
    /**
     * Toggles an item between visible states in the chart.
     *
     * @param {String} key the item to toggle
     */
    onClickLegendItem(key) {
        const dataset = this.chartConfig.data.datasets.find(ds => ds.label === key);
        if (!dataset) {
            return; //Handle error?
        }
        const isVisible = !dataset.hidden;
        // If we were using a custom top N, we need to update the visible_keys parameter
        if (this.config.nb_dataset !== -1) {
            this._pushCurrentVisibleKeys();
            this.config.nb_dataset = -1;
        }
        this.config.toggleVisibleKey(key);
        if (isVisible) {
            dataset.hidden = true;
        } else {
            dataset.hidden = false;
        }
        this.updateChart(false, true);
    }

    /**
     * Called when nb_dataset select field is changed.
     * 
     * @param {Event} ev the event
     */
    onChangeNbDataset(ev) {
        const { target } = ev;
        const value = parseInt(target.value);
        if (value === -1) {
            this._pushCurrentVisibleKeys();
        } else {
            this.config.pushVisibleKeys([]);
        }
        this.config.nb_dataset = value;
        this.updateChart(false, true);
    }

    /**
     * Selects the first build as the center build for the next fetch.
     */
    onClickPrevious() {
        const builds = Object.keys(this.state.data);
        if (!builds || !builds.length) {
            return
        }
        this.config.center_build_id = builds[0];
    }

    /**
     * Selects the last build as the center build for the next fetch.
     */
    onClickNext() {
        const builds = Object.keys(this.state.data);
        if (!builds || !builds.length) {
            return
        }
        this.config.center_build_id = builds[builds.length - 1];
    }
}

