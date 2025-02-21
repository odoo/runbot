/** @odoo-module **/

import { TextField } from "@web/views/fields/text/text_field";
import { CharField } from "@web/views/fields/char/char_field";
import { Many2OneField } from "@web/views/fields/many2one/many2one_field";

import { _lt } from "@web/core/l10n/translation";
import { formatDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { useInputField } from "@web/views/fields/input_field_hook";

import { useRef, xml, Component, markup} from "@odoo/owl";
import { useAutoresize } from "@web/core/utils/autoresize";
import { getFormattedValue } from "@web/views/utils";
import { UrlField } from "@web/views/fields/url/url_field";
import { X2ManyField , x2ManyField} from "@web/views/fields/x2many/x2many_field";
import { BooleanToggleField } from "@web/views/fields/boolean_toggle/boolean_toggle_field";


// https://stackoverflow.com/questions/4810841/pretty-print-json-using-javascript
function colorizeJson(json) {
    json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
        var cls = '';
        if (/^"/.test(match)) {
            if (/:$/.test(match)) {
                cls = 'o_runbot_json_key';
            } else {
                cls = 'o_runbot_json_value';
            }
        }
        return '<span class="' + cls + '">' + match + '</span>';
    });
}

function stringify(obj) {
        return JSON.stringify(obj, null, '\t');
    }
export class JsonField extends TextField {
    static template = xml`
    <t t-if="props.readonly">
            <span t-out="colorizedValue"/>
        </t>
        <t t-else="">
            <div t-ref="div">
                <textarea
                    class="o_input"
                    t-att-class="{'o_field_translate': props.isTranslatable}"
                    t-att-id="props.id"
                    t-att-placeholder="props.placeholder"
                    t-att-rows="rowCount"
                    t-ref="textarea"
                />
            </div>
        </t>
    `;
    setup() {
        this.divRef = useRef("div");
        this.textareaRef = useRef("textarea");
        //if (this.props.dynamicPlaceholder) {
        //    this.dynamicPlaceholder = useDynamicPlaceholder(this.textareaRef);
        //}

        useInputField({
            getValue: () => this.value,
            refName: "textarea",
            parse: JSON.parse,
        });
        useAutoresize(this.textareaRef, { minimumHeight: 50 });
    }

    get value() {
        return stringify(this.props.record.data[this.props.name] || "");
    }

    get colorizedValue() {
        return markup(colorizeJson(stringify(this.props.record.data[this.props.name] || "")));
    }
}

registry.category("fields").add("runbotjsonb", {
    component: JsonField,
});

export class FrontendUrl extends Component {
    static template = xml`
        <div><a t-att-href="route" target="_blank"><t t-out="displayValue"/></a></div>
    `;

    static components = { Many2OneField };

    static props = {
        ...Many2OneField.props,
        linkField: { type: String, optional: true },
    };

    get displayValue() {
        if (this.props.record.data[this.props.name].isLuxonDateTime){
            return formatDateTime(this.props.record.data[this.props.name])
        } else {
            return this.props.record.data[this.props.name] ? getFormattedValue(this.props.record, this.props.name) : ''
        }
    }

    get route() {
        return this._route(this.props.linkField || this.props.name)
    }

    _route(fieldName) {
        const model = this.props.record.fields[fieldName].relation || "runbot.unknown";
        const { id } = this.props.record.data[fieldName];
        if (model.startsWith('runbot.')){
            return '/runbot/' + model.split('.')[1] + '/' + id;
        } else {
            return false;
        }
    }
}

registry.category("fields").add("frontend_url", {
    supportedTypes: ["many2one", "datetime"],
    component: FrontendUrl,
    extractProps({ attrs, options }, dynamicInfo) {
        return {
            linkField: options.link_field,
        };
    },
});


export class FieldCharFrontendUrl extends Component {

    static template = xml`
    <div class="o_field_many2one_selection">
        <div class="o_field_widget"><CharField t-props="props" /></div>
        <div><a t-att-href="route" target="_blank"><span class="fa fa-play ms-2"/></a></div>
    </div>`;

    static components = { CharField }

    get route() {
        const model = this.props.record.resModel;
        const id = this.props.record.resId;
        if (model.startsWith('runbot.')) {
            return '/runbot/' + model.split('.')[1] + '/' + id;
        } else {
            return false;
        }
    }
}

registry.category("fields").add("char_frontend_url", {
    supportedTypes: ["char", "integer"],
    component: FieldCharFrontendUrl,
});


// Pull Request URL Widget
const pullRequestRegex = /\/([a-zA-Z-_]+\/[a-zA-Z-_]+)\/pull\/(\d+)/;
class PullRequestUrlField extends UrlField {
    static template = xml`
        <UrlField t-props="fieldProps"/>
    `;
    static components = { UrlField }
    get fieldProps() {
        const props = {...this.props };
        const parts = pullRequestRegex.exec(this.props.record.data[props.name])
        if (parts) {
            props.text = `${parts[1]}#${parts[2]}`;
        }
        return props
    }
}

PullRequestUrlField.supportedTypes = ["char"];


registry.category("fields").add("pull_request_url", {
    supportedTypes: ["char"],
    component: PullRequestUrlField,
});


export class Matrixx2ManyField extends X2ManyField {
    static template = 'runbot.Matrixx2ManyField';

    static components = { BooleanToggleField };

    getEntry(from, to) {
        return this.list.records.find(({ data }) => data.from_version_number === from && data.to_version_number === to);
    }

    get toVersions() {
        const versions = this.list.records.map(({ data }) => data.to_version_number);
        return [...new Set(versions)].sort();
    }

    get fromVersions() {
        const versions = this.list.records.map(({ data }) => data.from_version_number);
        return [...new Set(versions)].sort().reverse();
    }
}
export const matrixx2ManyField = {
    ...x2ManyField,
    component: Matrixx2ManyField,
    useSubView: false,
};


registry.category("fields").add("version_matrix", matrixx2ManyField);
