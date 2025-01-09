/** @odoo-module **/

import { Component, useEffect, useRef, useState } from '@odoo/owl';

import { debounce, filterKeys, randomColor } from '@runbot/utils';
import { useBus } from '@runbot/stats/use_bus';
import { useConfig, onConfigChange } from '@runbot/stats/use_config';
import { Chart } from '@runbot/chartjs';


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
                    const build_id = this.chartConfig.data.labels[activeElements[0].index];
                    if (shiftKey) {
                        this.config.center_build_id = build_id;
                    } else {
                        window.open(`/runbot/build/stats/${build_id}`);
                    }
                }
            },
        });

        onConfigChange(() => this.fetchStats(), true);
        useBus(this.env.bus, 'click-previous', () => this.selectPrevious());
        useBus(this.env.bus, 'click-next', () => this.selectNext());
        useEffect(() => {
            this.updateChart();
        }, () => [
            this.canvas, this.state.data,
            ...Object.values(filterKeys(this.config, this.config.getChartUpdateKeys()))
        ]);
    }

    /**
     * Called before actually fetching stat, this triggers the spinner while waiting
     * on the debounced _fetchStat.
     */
    fetchStats() {
        this.loading = true;
        this.env.bus.trigger('start-loading', {});
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
        this.env.bus.trigger('stop-loading', {});
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
        const { data } = this.state;
        const builds = Object.keys(data);
        const newestBuildStats = data[builds[builds.length - 1]];
        const oldestBuildStats = data[builds[0]];
        const keys = Object.keys(newestBuildStats);
        let idx = keys.indexOf('Aggregate Sum');
        if (aggregate === 'sum' && idx === -1) {
            keys.push('Aggregate Sum');
            Object.values(data).forEach((buildData) => {
                buildData['Aggregate Sum'] = Object.values(buildData).reduce((a, b) => a + b, 0);
            });
        } else if (aggregate !== 'sum' && idx !== -1) {
            keys.splice(idx, 1);
        }
        idx = keys.indexOf('Aggregate Average');
        if (aggregate === 'average' && idx === -1) {
            keys.push('Aggregate Average');
            Object.values(data).forEach((buildData) => {
                buildData['Aggregate Average'] = (Object.values(buildData).reduce((a, b) => a + b, 0) / Object.values(buildData).length);
            });
        } else if (aggregate !== 'average' && idx !== -1) {
            keys.splice(idx, 1);
        }
        // Mapping of keys to their sort value
        const sortValues = keys.reduce(
            (dict, key) => {
                const getValue = () => {
                    if (mode === 'normal') {
                        return newestBuildStats[key];
                    } else if (mode === 'alpha') {
                        return key;
                    } else if (mode === 'change_count') {
                        return builds.reduce((agg, build, buildIdx) => {
                            const currentBuild = data[build];
                            const current = currentBuild[key];
                            const previous = buildIdx === 0 ? undefined : data[builds[buildIdx - 1]][key];
                            if (previous !== undefined && current !== undefined && previous != current) {
                                agg += 1;
                            }
                            return agg;
                        }, 0);
                    } else if (mode === 'difference') {
                        return Math.abs(
                            newestBuildStats[key] - (oldestBuildStats[key] || 0)
                        );
                    }
                }
                dict[key] = getValue();
                return dict;
            }, {},
        );
        keys.sort((k1, k2) => sortValues[k2] - sortValues[k1]);
        let visibleKeys;
        if (this.config.nb_dataset !== -1) {
            visibleKeys = new Set(keys.slice(0, this.config.nb_dataset));
        } else {
            visibleKeys = new Set(this.config.getVisibleKeys());
        }
        const getDisplayValue = (key, build) => {
            if (build[key] === undefined) {
                return NaN;
            }
            if (mode === 'normal' || mode === 'alpha') {
                return build[key];
            }
            return build[key] - (oldestBuildStats[key] || 0)
        }
        this.chartConfig.data = {
            labels: builds,
            datasets: keys.map((key) => ({
                label: key,
                data: builds.map(build => getDisplayValue(key, data[build])),
                borderColor: randomColor(key),
                backgroundColor: 'rgba(0, 0, 0, 0)',
                lineTension: 0,
                hidden: !visibleKeys.has(key),
            })),
        };
    }

    /**
     * Compute chart data and trigger an update on the chart.
     * If the canvas is not set, nothing happens.
     *
     * @param {Boolean} recompute whether to recompute the chart's dataset or not.
     */
    updateChart(recompute = true) {
        if (!this.canvas || !this.canvas.el) {
            return
        }
        if (recompute) {
            this._computeChartData();
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
    }

    /**
     * Selects the first build as the center build for the next fetch.
     */
    selectPrevious() {
        const builds = Object.keys(this.state.data);
        if (!builds || !builds.length) {
            return
        }
        this.config.center_build_id = builds[0];
    }

    /**
     * Selects the last build as the center build for the next fetch.
     */
    selectNext() {
        const builds = Object.keys(this.state.data);
        if (!builds || !builds.length) {
            return
        }
        this.config.center_build_id = builds[builds.length - 1];
    }
}

