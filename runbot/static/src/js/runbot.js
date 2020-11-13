(function($) {
    "use strict";

    var OPMAP = {
        'rebuild': {operation: 'rebuild', then: 'redirect'},
        'kill': {operation: 'kill', then: 'reload'},
        'wakeup': {operation: 'wakeup', then: 'reload'}
    };

    $(function () {
        $(document).on('click', '[data-runbot]', function (e) {
            e.preventDefault();

            var data = $(this).data();
            var segment = OPMAP[data.runbot];
            if (!segment) { return; }

            // window.location.pathname but compatibility is iffy
            var currentPath = window.location.href.replace(window.location.protocol + '//' + window.location.host, '').split('?')[0];
            var buildPath = _.str.sprintf('/runbot/build/%s', data.runbotBuild);
            // no responseURL on $.ajax so use native object
            var xhr = new XMLHttpRequest();
            xhr.addEventListener('load', function () {
                switch (segment.then) {
                case 'redirect':
                    if (currentPath === buildPath && xhr.responseURL) {
                        window.location.href = xhr.responseURL;
                        break;
                    }
                // fallthrough to reload if no responseURL or we're
                // not on the build's page
                case 'reload':
                    window.location.reload();
                    break;
                }
            });
            xhr.open('POST', _.str.sprintf('%s/%s', buildPath, segment.operation));
            xhr.send();
        });
    });
    //$(function() {
    //  new Clipboard('.clipbtn');
    //});
})(jQuery);

function copyToClipboard(text) {
        if (!navigator.clipboard) {
            console.error('Clipboard not supported');
            return;
        }
        navigator.clipboard.writeText(text);
    }
