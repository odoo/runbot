import { Component, xml } from "@odoo/owl";

export class StatsChartLegend extends Component {
    static props = {
        items: Array,
        onClickItem: Function,
    };
    static template = xml`
        <ul class="chart-legend list-unstyled overflow-y-auto">
            <t t-log="props.items"/>
            <li t-foreach="this.props.items" t-as="item" t-key="item.label" class="chart-legend-item ps-1 fw-bold text-truncate" t-att-class="{disabled: item.hidden}" t-attf-style="--chart-legend-item-accent: {{item.borderColor}}" t-att-title="item.label" t-on-click="this.onClickItem(item)"/>
        </ul>
    `;
}
