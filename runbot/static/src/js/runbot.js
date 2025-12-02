// @odoo-module ignore

function copyToClipboard(text) {
    if (!navigator.clipboard) {
        console.error('Clipboard not supported');
        return;
    }
    navigator.clipboard.writeText(text);
}
