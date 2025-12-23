import { Component, xml, onWillStart, onWillDestroy, onWillRender, useRef, useEffect, useState } from "@odoo/owl";
import { loadJS } from "@web/core/assets";

export class StatsChart extends Component {
    static props = {
        data: Object,
        config: Object,
        state: Object,
    };
    static template = xml`
        <div class="chart-container position-relative">
            <canvas t-ref="canvas"/>
            <div t-if="this.loader.isLoading" class="position-absolute top-0 bottom-100 start-0 end-100 h-100 w-100 bg-white bg-opacity-75">
                <i class="position-absolute top-50 start-50 fa fa-2x fa-fw fa-circle-o-notch fa-spin"/>
            </div>
        </div>
    `;

    setup() {
        this.canvas = useRef("canvas");
        this.data = useState(this.props.data);
        this.loader = useState(this.props.loader);
        onWillStart(async () => {
            if (!("Chart" in window)) {
                await loadJS("/web/static/lib/Chart/Chart.js");
            }
        });
        onWillDestroy(() => {
            this.chart.destroy();
        });
        useEffect((el) => {
            this.chart = new Chart(el, this.props.config);
        }, () => [this.canvas.el]);
        onWillRender(() => {
            if (!this.chart) {
                return;
            }
            this.chart.data = { ...this.data };
            this.chart.update();
        });
    }
}
