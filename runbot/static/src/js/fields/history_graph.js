/** @odoo-module **/
import { _lt } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useRef, xml, Component, useEffect } from "@odoo/owl";

export class HistoryGraph extends Component {
    static template = xml`
        <div class="w-100">
            <canvas t-ref="canvas"/>
        </div>
    `;
    setup() {
        this.canvasRef = useRef("canvas");
        this.data = this.props.record.data[this.props.name] || {};
        this.errorId = this.data.error_id;
        this.projectId = this.data.project_id;
        this.categoryId = this.data.category_id;
        this.breaking_pr_close_date = this.data.breaking_pr_close_date;
        this.fixing_pr_close_date = this.data.fixing_pr_close_date;
        this.breaking_pr_version_label = this.data.breaking_pr_version_label;
        this.fixing_pr_version_label = this.data.fixing_pr_version_label;
        useEffect(() => this.renderErrorGraph());
    }

    renderErrorGraph(activeCell) {
        const canvas = this.canvasRef.el
        const ctx = canvas.getContext("2d");
        const maxValue = this.data.max_count;
        const canvasBorder = 1;
        const cellBorder = 0.5;
        const cellSize = this.props.cellSize;
        const mouseActions = this.props.mouseActions;
        const cellWidth = cellSize - cellBorder * 2;
        const cellHeight = cellSize - cellBorder * 2;
        const canvasWidth = this.data.x_labels.length * cellSize + canvasBorder * 2;
        const canvasHeight = this.data.y_labels.length * cellSize + canvasBorder * 2;
        canvas.width = canvasWidth;
        canvas.height = canvasHeight;


        function getColor(value, opacity) {
            if (value >= 10) {
                return `rgba(255, 0, 0, ${opacity})`; // red
            } else if (value >= 5) {
                return `rgba(255, 165, 0, ${opacity})`; // orange
            }
            return `rgba(0, 170, 0, ${opacity})` // green
        }

        ctx.clearRect(0, 0, canvasWidth, canvasHeight);
        ctx.fillStyle = "#EEE";
        ctx.fillRect(0, 0, canvasWidth, canvasHeight);
        ctx.strokeStyle = "#333";
        ctx.lineWidth = canvasBorder * 2; // * 2 to account for each side, not only inner width 
        ctx.strokeRect(0, 0, canvasWidth, canvasHeight,);

        this.data.x_labels.forEach((xLabel, idx) => {
            this.data.y_labels.forEach((yLabel, idy) => {
                let value = this.data.daily_version_freq[idx][idy] || 0;
                let cellColor = "white";
                let cellOpacity = 0;
                if (value) {
                    value = Math.min(value, maxValue);
                    cellOpacity = ((maxValue * 0.3 + value) / (maxValue * 0.3 + maxValue));
                    cellColor = getColor(value, cellOpacity);
                }
                const posX = idx * cellSize + canvasBorder + cellBorder;
                const posY = idy * cellSize + canvasBorder + cellBorder;

                ctx.fillStyle = cellColor;
                ctx.fillRect(posX, posY, cellWidth, cellHeight);
                if (activeCell && activeCell.col === idx && activeCell.row === idy) {
                    ctx.strokeStyle = "black";
                    ctx.lineWidth = 2;
                    ctx.strokeRect(posX, posY, cellWidth, cellHeight);
                }


                if (this.fixing_pr_close_date == xLabel && this.fixing_pr_version_label == yLabel) {
                    console.log(this.fixing_pr_version_label, yLabel)
                    ctx.fillStyle = "black";
                    ctx.font = "12px Arial";
                    ctx.fillText("✓", posX + cellWidth / 2 - 4, posY + cellHeight / 2 + 4);
                }
                if (this.breaking_pr_close_date == xLabel && this.breaking_pr_version_label == yLabel) {
                    ctx.fillStyle = "black";
                    ctx.font = "12px Arial";
                    ctx.fillText("✗", posX + cellWidth / 2 - 4, posY + cellHeight / 2 + 4);
                }


            });
        });
        console.log(mouseActions)
        if (mouseActions) {
            canvas.onmousemove = (event) => {
                let tooltip = canvas.parentElement.querySelector('.history-graph-tooltip');
                if (tooltip) {
                    tooltip.remove();
                }

                const { col, row, value, xLabel, yLabel } = this.getCellFromEvent(event);

                if ( col >= 0 && row >= 0) {
                    tooltip = document.createElement('div');
                    tooltip.className = 'history-graph-tooltip';
                    tooltip.style.position = 'absolute';
                    tooltip.style.left = `${canvas.offsetLeft}px`;
                    tooltip.style.top = `${canvas.offsetTop + canvas.height}px`;
                    tooltip.style.background = '#fff';
                    tooltip.style.border = '1px solid #333';
                    tooltip.style.padding = '4px 8px';
                    tooltip.style.fontSize = '12px';
                    tooltip.style.pointerEvents = 'none';
                    tooltip.style.zIndex = 1000;
                    tooltip.innerHTML = `
                        Date: ${xLabel}
                        Version: ${yLabel}
                        Value: ${value}
                    `;
                    canvas.parentElement.appendChild(tooltip);
                    this.renderErrorGraph({ col, row }); // Re-render to highlight the active cell
                } else {
                    this.renderErrorGraph();
                }
            };

            canvas.onmouseleave = () => {
                const tooltip = canvas.parentElement.querySelector('.history-graph-tooltip');
                if (tooltip) {
                    tooltip.remove();
                    this.renderErrorGraph()
                }
            };

            canvas.onclick = (event) => {
                const { col, row, value, xLabel, yLabel } = this.getCellFromEvent(event);
                if (col >= 0 && row >= 0) {
                    const url = `/runbot/batches/${this.projectId}/${this.categoryId}/${xLabel}/${this.errorId}`;
                    window.open(url, '_blank');
                }
            }
        }

    }
    getCellFromEvent(event) {
        const rect = this.canvasRef.el.getBoundingClientRect();
        const x = event.clientX - rect.left - 1; // Adjust for canvas border
        const y = event.clientY - rect.top - 1; // Adjust for canvas border
        const col = Math.floor(x / this.props.cellSize);
        const row = Math.floor(y / this.props.cellSize);
         if ( col >= 0 && col < this.data.x_labels.length && row >= 0 && row < this.data.y_labels.length) {
            const value = this.data.daily_version_freq[col][row] || 0;
            const xLabel = this.data.x_labels[col];
            const yLabel = this.data.y_labels[row];
            return { col, row, value, xLabel, yLabel };
        } else {
            return { col: -1, row: -1, value: 0, xLabel: '', yLabel: '' };
        }
    }
}

registry.category("fields").add("history_graph", {
    supportedTypes: ["jsonb"],
    component: HistoryGraph,
    extractProps({ attrs, options }, dynamicInfo) {
        return {
            cellSize: options.cell_size || 5, // Default cell size if not specified
            mouseActions: options.mouse_actions || false, // Default to false if not specified
        };
    },
});
