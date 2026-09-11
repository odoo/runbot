// @odoo-module ignore

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
