import publicWidget from "@web/legacy/js/public/public_widget";
import { debounce } from "@web/core/utils/timing";
import { cookie } from "@web/core/browser/cookie";
import { ManagePreferencesDialog } from "@runbot/js/manage_preferences_dialog";
// import { FormErrorDialog } from "@web/views/form/form_error_dialog/form_error_dialog";


publicWidget.registry.RunbotPage = publicWidget.Widget.extend({
    // This selector should not be so broad.
    selector: 'body',
    events: {
        'click [data-runbot]': '_onClickDataRunbot',
        'click [data-runbot-clipboard]': '_onClickRunbotCopy',
        'click .o_runbot_copy_link': '_onClickCopyLink',
    },

    start: function () {
        this._super(...arguments);

        // If we have a hash, try to animate the hashed id
        const hash = window.location.hash.substring(1);
        if (hash.length) {
            const elem = document.getElementById(hash);
            if (elem) {
                elem.classList.add('fa-bounce', 'text-bg-warning');
            }
        }
    },

    _onClickDataRunbot: async (event) => {
        const { currentTarget: target } = event;
        const { runbot: operation, runbotBuild } = target.dataset;
        if (!operation) {
            return;
        }
        event.preventDefault();
        let url = target.href;
        if (runbotBuild) {
            url = `/runbot/build/${runbotBuild}/${operation}`
        }
        const response = await fetch(url, {
            method: 'POST',
        });
        if (operation == 'rebuild' && window.location.href.split('?')[0].endsWith(`/build/${runbotBuild}`)) {
            window.location.href = window.location.href.replace('/build/' + runbotBuild, '/build/' + await response.text());
        } else if (operation == 'action') {
            target.parentElement.innerText = await response.text();
        } else {
            window.location.reload();
        }
    },

    _writeClipboard: function (text) {
        return navigator.clipboard.writeText(text);
    },

    _onClickRunbotCopy: function ({ currentTarget: target }) {
        if (!navigator.clipboard) {
            return;
        }
        this._writeClipboard(
            target.dataset.runbotClipboard
        );
    },

    _onClickCopyLink: function (event) {
        if (event.altKey || event.ctrlKey || event.metaKey) {
            return;
        }
        const { currentTarget: target } = event;
        // Check meta keys and stuff
        event.preventDefault();
        this._writeClipboard(target.href);
    }
});

// Set initial theme on page load
document.documentElement.dataset.bsTheme = localStorage.getItem('runbotTheme') || 'light';

publicWidget.registry.ThemeSwitcher = publicWidget.Widget.extend({
    selector: '.o_runbot_preferences',
    events: {
        'change .o_runbot_theme_switcher': '_onChangeTheme',
        'click .o_runbot_more_info': '_onChangeMoreInfo',
        'click .o_runbot_manage_filters': '_onClickManageFilters',
    },

    init: function () {
        this._super(...arguments);
        this.theme = localStorage.getItem('runbotTheme') || 'light';
        document.documentElement.dataset.bsTheme = this.theme;
        this._onChangeMoreInfo = debounce(this._onChangeMoreInfo, 300).bind(this);
    },

    start: function () {
        this.moreInfoEl = this.el.querySelector('.o_runbot_more_info');
        this.dropdownMenu = this.el.querySelector('.dropdown-menu');
        this.el.querySelector('.o_runbot_theme_switcher').value = this.theme;
    },

    _onChangeTheme: ({ currentTarget: target }) => {
        this.theme = target.value;
        document.documentElement.dataset.bsTheme = this.theme;
        localStorage.setItem('runbotTheme', this.theme);
    },

    _onChangeMoreInfo: function () {
        const { checked } = this.moreInfoEl;
        const cookieChecked = cookie.get('more');
        if (checked && !cookieChecked) {
            cookie.set('more', 'true');
        } else if (!checked && cookieChecked) {
            cookie.delete('more');
        } else {
            return;
        }
        location.reload();
    },

    _onClickManageFilters: function (event) {
        event.preventDefault();
        this.dropdownMenu.classList.remove('show');
        this.call('dialog', 'add', ManagePreferencesDialog);
    },
});

publicWidget.registry.RunbotToolbar = publicWidget.Widget.extend({
    selector: '.o_runbot_toolbar.position-sticky',

    start: function () {
        this._super();

        const navbarElem = document.querySelector('nav.navbar');
        if (!navbarElem) {
            return;
        }
        this.resizeObserver = new ResizeObserver(() => {
            this.el.style.top = navbarElem.getBoundingClientRect().height;
        });
        this.resizeObserver.observe(this.el);
    },

    destroy: function () {
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
        }
        this._super();
    }
});
