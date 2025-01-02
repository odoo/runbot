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

/**
 * Shamelessly stolen from owl's code, execute a function when the DOM is ready.
 *
 * @param {*} fn function to call when the DOM is ready.
 * @returns {Promise} Promise that can be awaited for after DOM is ready.
 */
function whenReady(fn) {
    return new Promise(function (resolve) {
        if (document.readyState !== "loading") {
            resolve(true);
        } else {
            document.addEventListener("DOMContentLoaded", resolve, false);
        }
    }).then(fn || function () { });
}

// Hidden checkbox with keyboard support
whenReady(() => {
    Array.from(
        document.querySelectorAll('label.o_runbot_hidden_checkbox')
    ).filter(
        (label) => !!label.control
    ).forEach(
        (label) => {
            label.addEventListener(
                'keydown', (event) => {
                    const { key } = event;
                    if (key === ' ') {
                        label.control.checked = !label.control.checked;
                        event.preventDefault();
                    } else if (key === 'Enter') {
                        label.closest('form').submit();
                    }
                }
            );
        }
    );
})
