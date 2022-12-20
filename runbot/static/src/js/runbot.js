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
