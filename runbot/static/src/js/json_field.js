odoo.define('runbot.json_field', function (require) {
"use strict";
    
var basic_fields = require('web.basic_fields');
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
console.log(field_utils);

function stringify(obj) {
    return JSON.stringify(obj, null, '\t')
}
field_utils.format.jsonb = stringify;
field_utils.parse.jsonb = JSON.parse;
});