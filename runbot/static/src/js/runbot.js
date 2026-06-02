// @odoo-module ignore
(function($) {
    "use strict";   
    $(function () {
        $(document).on('click', '[data-runbot]', function (e) {
            e.preventDefault();
            var data = $(this).data();
            var operation = data.runbot;
            if (!operation) { 
                return; 
            }
            var xhr = new XMLHttpRequest();
            var url = e.target.href
            if (data.runbotBuild) {
                url = '/runbot/build/' + data.runbotBuild + '/' + operation
            }
            var elem = e.target 
            xhr.addEventListener('load', function () {
                if (operation == 'rebuild' && window.location.href.split('?')[0].endsWith('/build/' + data.runbotBuild)){
                    window.location.href = window.location.href.replace('/build/' + data.runbotBuild, '/build/' + xhr.responseText);
                } else if (operation == 'action') {
                    elem.parentElement.innerText = this.responseText
                } else {
                    window.location.reload();
                }
            });
            xhr.open('POST', url);
            xhr.send();
        });
    });
})(jQuery);


function copyToClipboard(text) {
    if (!navigator.clipboard) {
        console.error('Clipboard not supported');
        return;
    }
    navigator.clipboard.writeText(text);
}

document.addEventListener('DOMContentLoaded', function() {
    const collapseElement = document.getElementById('customTriggers');
    if (collapseElement) {
        collapseElement.addEventListener('show.bs.collapse', function () {
            const url = new URL(window.location);
            url.searchParams.set('expand_custom', '1');
            window.history.replaceState({}, '', url);
        });
        collapseElement.addEventListener('hide.bs.collapse', function () {
            const url = new URL(window.location);
            url.searchParams.delete('expand_custom');
            window.history.replaceState({}, '', url);
        });
    }
});

// Expand/collapse all team sections at once.
document.addEventListener('DOMContentLoaded', function() {
    const expandBtn = document.getElementById('toggleTeamsButton');
    if (expandBtn) {
        const bundleGrid = document.querySelector('.o_team_bundle_grid');
        const container = bundleGrid.closest('.container-fluid');
        expandBtn.addEventListener('click', function () {
            const details = container.querySelectorAll('details');
            const allOpen = Array.from(details).every(d => d.open);
            details.forEach(d => d.open = !allOpen);
            this.textContent = allOpen ? 'Expand all' : 'Collapse all';
        });
    }
});
