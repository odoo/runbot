// @odoo-module ignore

document.addEventListener("click", function (e) {
    const elem = e.target.closest("[data-runbot]");
    if (!elem) {
        return;
    }
    e.preventDefault();
    const data = elem.dataset;
    var operation = data.runbot;
    if (!operation) {
        return;
    }
    var xhr = new XMLHttpRequest();
    let url = elem.href;
    if (data.runbotBuild) {
        url = '/runbot/build/' + data.runbotBuild + '/' + operation
    }
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

document.addEventListener('click', function (e) {
    const button = e.target.closest('[data-copy-text]');
    if (!button) {
        return;
    }
    if (!navigator.clipboard) {
        console.error('Clipboard not supported');
        return;
    }
    navigator.clipboard.writeText(button.dataset.copyText);
});

document.addEventListener('click', function (e) {
    const button = e.target.closest('[data-toggle="hide-success"]');
    if (!button) {
        return;
    }
    const hidden = document.documentElement.classList.toggle('hide-success');
    button.setAttribute('aria-expanded', String(!hidden));
});

document.addEventListener('click', function (e) {
    const toggler = e.target.closest('[data-toggle="limited-height"]');
    if (!toggler) {
        return;
    }
    document.querySelector(toggler.dataset.target)?.classList.toggle('limited-height');
});

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
