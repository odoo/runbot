/** @typedef {import("./data_table").DataTable} DataTable */

/**
 * Base class of the optional plugins of a `data-table`, enabled by a `data-<name>` attribute on the element.
 */
export class DataTablePlugin {
    /**
     * @param {DataTable} dataTable
     */
    constructor(dataTable) {
        this.dataTable = dataTable;
    }

    connect() {}

    disconnect() {}
}
