import { Component, xml, useState } from "@odoo/owl";

export class StatsChartLegend extends Component {
    static props = {
        data: Object,
        onClickItem: Function,
    };
    static template = xml`
        <ul class="chart-legend list-unstyled overflow-y-auto">
            <t t-foreach="this.data.datasets" t-as="item" t-key="item.label">
                <li class="chart-legend-item ps-1 fw-bold text-truncate" t-att-class="{disabled: item.hidden}" t-attf-style="--chart-legend-item-accent: {{item.borderColor}}" t-att-title="item.label" t-on-click="this.props.onClickItem(item)" t-out="item.label"/>
            </t>
        </ul>
    `;
    setup() {
        this.data = useState(this.props.data);
    }
}
