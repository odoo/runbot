import { Component, xml, onWillStart, onWillDestroy, useRef, useEffect } from "@odoo/owl";
import { loadJS } from "@web/core/assets";

export class StatsChart extends Component {
    static props = {
        data: Object,
        config: Object,
        isLoading: { type: Boolean, optional: true },
    };
    static template = xml`
        <div class="chart-container position-relative">
            <canvas t-ref="canvas"/>
            <div t-if="this.props.isLoading" class="position-absolute top-0 bottom-100 start-0 end-100 h-100 w-100 bg-white bg-opacity-75">
                <i class="position-absolute top-50 start-50 fa fa-2x fa-fw fa-circle-o-notch fa-spin"/>
            </div>
        </div>
    `;

    setup() {
        this.canvas = useRef("canvas");
        onWillStart(async() => {
            if (!("Chart" in window)) {
                await loadJS("/web/static/lib/Chart/Chart.js");
            }
        });
        onWillDestroy(() => {
            this.chart.destroy();
        });
        useEffect((el) => {
            console.debug("useEffect#init", this.chartConfig);
            this.chart = new Chart(el, this.chartConfig);
        }, () => [this.canvas.el]);
        useEffect(() => {
            console.log("useEffect#update", this.chartConfig);
            if (!this.chart) {
                return;
            }
            this.chart.data = this.props.data;
            this.chart.update();
        }, () => [this.props.data.stats, this.props.data.dates]);
    }

    get chartConfig() {
        return {
            ...this.props.config,
            data: this.props.data,
        };
    }
}
