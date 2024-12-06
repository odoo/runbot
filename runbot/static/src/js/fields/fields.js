/** @odoo-module **/

import { TextField } from "@web/views/fields/text/text_field";
import { CharField } from "@web/views/fields/char/char_field";
import { Many2OneField } from "@web/views/fields/many2one/many2one_field";

import { _lt } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useDynamicPlaceholder } from "@web/views/fields/dynamic_placeholder_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useInputField } from "@web/views/fields/input_field_hook";

import { useRef, xml, Component } from "@odoo/owl";
import { useAutoresize } from "@web/core/utils/autoresize";
import { getFormattedValue } from "@web/views/utils";

import { UrlField } from "@web/views/fields/url/url_field";

function stringify(obj) {
    return JSON.stringify(obj, null, '\t')
}


export class JsonField extends TextField {
    static template = xml`
    <t t-if="props.readonly">
            <span t-out="value"/>
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
}

registry.category("fields").add("runbotjsonb", {
    supportedTypes: ["jsonb"],
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

    get baseProps() {
        console.log(omit(this.props, 'linkField'))
        return omit(this.props, 'linkField', 'context')
    }

    get displayValue() {
        return this.props.record.data[this.props.name] ? getFormattedValue(this.props.record, this.props.name) : ''
    }

    get route() {
        return this._route(this.props.linkField || this.props.name)
    }

    _route(fieldName) {
        const model = this.props.record.fields[fieldName].relation || "runbot.unknown";
        const id = this.props.record.data[fieldName][0];
        if (model.startsWith('runbot.') ) {
            return '/runbot/' + model.split('.')[1] + '/' + id;
        } else {
            return false;
        }
    }
}

registry.category("fields").add("frontend_url", {
    supportedTypes: ["many2one"],
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
        if (model.startsWith('runbot.') ) {
            return '/runbot/' + model.split('.')[1] + '/' + id;
        } else {
            return false;
        }
    }
}

registry.category("fields").add("char_frontend_url", {
    supportedTypes: ["char"],
    component: FieldCharFrontendUrl,
});


// Pull Request URL Widget
class PullRequestUrlField extends UrlField {
    static template = xml`
    <a class="o_field_widget o_form_uri" t-on-click.stop="" t-att-href="formattedHref" t-esc="props.record.data[props.name].replace('https://github.com/odoo','').replace('/pull','') || ''" target="_blank"/>
    `;
    static components = { UrlField }
    setup() {
        if (!this.props.readonly) {
            throw new Error("This widget works only on readonly fields");
        }
    }
}

PullRequestUrlField.supportedTypes = ["char"];


registry.category("fields").add("pull_request_url", {
    supportedTypes: ["char"],
    component: PullRequestUrlField,
});


//export class GithubTeamWidget extends CharField {

//this.value.split(',').forEach((value) => {
//    const href = 'https://github.com/orgs/' + organisation + '/teams/' + value.trim() + '/members';
//}
//
//registry.category("fields").add("github_team", GithubTeamWidget);
