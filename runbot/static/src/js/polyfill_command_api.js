// @odoo-module ignore
(function () {
    if (
        typeof HTMLButtonElement !== "undefined" &&
            "command" in HTMLButtonElement.prototype &&
            // eslint-disable-next-line no-undef
            "source" in ((CommandEvent || {}).prototype || {})
    ) {
        return;
    }
    const SUPPORTED_COMMANDS = {
        "show-modal": "showModal",
        "close": "close",
    };
    document.addEventListener("click", (ev) => {
        const commandEl = ev.target.closest("[commandfor]");
        if (!commandEl) {
            return;
        }
        const forTarget = document.getElementById(commandEl.getAttribute("commandfor"));
        const command = commandEl.getAttribute("command");
        if (command in SUPPORTED_COMMANDS) {
            forTarget[SUPPORTED_COMMANDS[command]]();
        } else {
            throw new Error(`UnsupportedCommand: ${command} is not a supported command.`);
        }
    });
})();
