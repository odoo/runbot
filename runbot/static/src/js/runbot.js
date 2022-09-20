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
            xhr.addEventListener('load', function () {
                if (operation == 'rebuild' && window.location.href.split('?')[0].endsWith('/build/' + data.runbotBuild)){
                    window.location.href = window.location.href.replace('/build/' + data.runbotBuild, '/build/' + xhr.responseText);
                } else {
                    window.location.reload();
                }
            });
            xhr.open('POST', '/runbot/build/' + data.runbotBuild + '/' + operation);
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

function toggleMoreInfo() {
    let batches_commits_infos = document.getElementsByClassName("batch_commits");
    for (let i = 0; i < batches_commits_infos.length; i++) {
        info_block = batches_commits_infos[i];
        console.log(info_block.style.display);
        if (info_block.style.display === "none") {
            info_block.style.display = "block";
        }
        else {
            info_block.style.display = "none";
        }
    }
}