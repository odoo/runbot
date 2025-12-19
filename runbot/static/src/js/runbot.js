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

document.addEventListener("DOMContentLoaded", function letItSnow() {
    const NB_SNOWFLAKES = 25;
    for (let i = 0; i < NB_SNOWFLAKES; i++) {
        const snowflake = document.createElement("div");
        snowflake.classList.add("snowflake");
        snowflake.style.setProperty("--snowflake-seed", Math.floor(Math.random() * NB_SNOWFLAKES));
        snowflake.style.setProperty("--snowflake-count", NB_SNOWFLAKES);
        const icon = document.createElement("i");
        icon.classList.add("inner", "fa", "fa-snowflake-o");
        snowflake.appendChild(icon);
        document.body.appendChild(snowflake);
    }
});
