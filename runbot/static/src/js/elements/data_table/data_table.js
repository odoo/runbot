import { FilterPlugin } from "./filter_plugin";
import { GroupPlugin } from "./group_plugin";

/** @typedef {import("./plugin").DataTablePlugin} DataTablePlugin */

/**
 * Wraps a table, enabling the registered plugins set on it, e.g. `<data-table data-group="true">`.
 */
export class DataTable extends HTMLElement {
    static plugins = new Map();

    /**
     * @param {string} name name of the `data-<name>` attribute enabling the plugin
     * @param {typeof DataTablePlugin} Plugin
     */
    static registerPlugin(name, Plugin) {
        this.plugins.set(name, Plugin);
    }

    connectedCallback() {
        this.activePlugins = [];
        for (const [name, Plugin] of this.constructor.plugins) {
            if (name in this.dataset) {
                const plugin = new Plugin(this);
                plugin.connect();
                this.activePlugins.push(plugin);
            }
        }
    }

    disconnectedCallback() {
        for (const plugin of this.activePlugins) {
            plugin.disconnect();
        }
        this.activePlugins = [];
    }

    // body rows, without the header ones
    get rows() {
        return [...this.querySelectorAll("tbody > tr:not(:has(th))")];
    }
}

// registered before defining the element, as the tables already in the page get connected right away
DataTable.registerPlugin("filter", FilterPlugin);
DataTable.registerPlugin("group", GroupPlugin);

customElements.define("data-table", DataTable);
