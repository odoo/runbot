odoo.define('runbot.json_field', function (require) {
"use strict";
    
var basic_fields = require('web.basic_fields');
var relational_fields = require('web.relational_fields');
var registry = require('web.field_registry');
var field_utils = require('web.field_utils');
var dom = require('web.dom');


var FieldJson = basic_fields.FieldChar.extend({
    init: function () {
        this._super.apply(this, arguments);

        if (this.mode === 'edit') {
            this.tagName = 'textarea';
        }
        this.autoResizeOptions = {parent: this};
    },

    start: function () {
        if (this.mode === 'edit') {
            dom.autoresize(this.$el, this.autoResizeOptions);
        }
        return this._super();
    },
    _onKeydown: function (ev) {
        if (ev.which === $.ui.keyCode.ENTER) {
            ev.stopPropagation();
            return;
        }
        this._super.apply(this, arguments);
    },

});

registry.add('jsonb', FieldJson)

var FieldCharFrontendUrl = basic_fields.FieldChar.extend({
    quickEditExclusion: [
        '.fa-external-link',
    ],
    init() {
        this._super.apply(this, arguments);
        if (this.model.startsWith('runbot.')) {
            this.route = '/runbot/' + this.model.split('.')[1] + '/' + this.res_id;
        } else {
            this.route = false;
        }
    },
    _renderReadonly: function() {
        this._super.apply(this, arguments);
        var link= '';
        if (this.route) {
            link = ' <a href="'+this.route+'" target="_blank"><i class="external_link fa fa-fw o_button_icon fa-external-link "/></a>';
            this.$el.html('<span>' + this.$el.html() + link + '<span>');
        }
    }
});

registry.add('char_frontend_url', FieldCharFrontendUrl)

var FrontendUrl = relational_fields.FieldMany2One.extend({
    isQuickEditable: false,
    events: _.extend({'click .external_link': '_stopPropagation'}, relational_fields.FieldMany2One.prototype.events),
    init() {
        this._super.apply(this, arguments);
        if (this.value) {
            const model = this.value.model.split('.').slice(1).join('_');
            const res_id = this.value.res_id;
            this.route = '/runbot/' + model+ '/' + res_id;
        } else {
            this.route = false;
        }
    },
    _renderReadonly: function () {
        this._super.apply(this, arguments);
        var link = ''
        if (this.route) {
            link = ' <a href="'+this.route+'" target="_blank"><i class="external_link fa fa-fw o_button_icon fa-external-link "/></a>'
        }
        this.$el.html('<span>' + this.$el.html() + link + '<span>')
    },
    _stopPropagation: function(event) {
        event.stopPropagation()
    }
});
registry.add('frontend_url', FrontendUrl)

function stringify(obj) {
    return JSON.stringify(obj, null, '\t')
}

field_utils.format.jsonb = stringify;
field_utils.parse.jsonb = JSON.parse;

var GithubTeamWidget = basic_fields.InputField.extend({
    events: _.extend({'click': '_onClick'}, basic_fields.InputField.prototype.events),
    _renderReadonly: function () {
        if (!this.value) {
            return;
        }
        this.el.textContent = '';
        const organisation = this.record.data.organisation;
        this.value.split(',').forEach((value) => {
            const href = 'https://github.com/orgs/' + organisation + '/teams/' + value.trim() + '/members';
            const anchorEl = Object.assign(document.createElement('a'), {
                text: value + " ",
                href: href,
                target: '_blank',
            });
            this.el.appendChild(anchorEl);
        });
      
    },

    /**
     * Prevent the URL click from opening the record (when used on a list).
     * @private
     * @param {MouseEvent} ev
     */
    _onClick: function (ev) {
        ev.stopPropagation();
    },
});
registry.add('github_team', GithubTeamWidget)

});
