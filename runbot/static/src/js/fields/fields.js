/** @odoo-module **/

import { TextField } from "@web/views/fields/text/text_field";
import { CharField } from "@web/views/fields/char/char_field";
import { Many2OneField } from "@web/views/fields/many2one/many2one_field";

import { _lt } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useDynamicPlaceholder } from "@web/views/fields/dynamicplaceholder_hook";
import { useInputField } from "@web/views/fields/input_field_hook";

import { onMounted, onWillUnmount, useEffect, useRef, xml, Component } from "@odoo/owl";


function stringify(obj) {
    return JSON.stringify(obj, null, '\t')
}


export class JsonField extends TextField {
    static template = xml`
    <t t-if="props.readonly">
            <span t-esc="value"/>
        </t>
        <t t-else="">
            <div t-ref="div">
                <textarea
                    class="o_input"
                    t-att-class="{'o_field_translate': props.isTranslatable}"
                    t-att-id="props.id"
                    t-att-placeholder="props.placeholder"
                    t-att-rows="rowCount"
                    t-on-input="onInput"
                    t-ref="textarea"
                />
            </div>
        </t>
    `;
    setup() {
        if (this.props.dynamicPlaceholder) {
            this.dynamicPlaceholder = useDynamicPlaceholder();
        }
        this.divRef = useRef("div");
        this.textareaRef = useRef("textarea");

        useInputField({
            getValue: () => this.value,
            refName: "textarea",
            parse: JSON.parse,
        });

        useEffect(() => {
            if (!this.props.readonly) {
                this.resize();
            }
        });
        onMounted(this.onMounted);
        onWillUnmount(this.onWillUnmount);
    }
    get value() {
        return stringify(this.props.value || "");
    }
}

registry.category("fields").add("jsonb", JsonField);

export class FrontendUrl extends Component {
    static template = xml`
    <div class="o_field_many2one_selection">
        <div class="o_field_widget"><Many2OneField t-props="props"/></div>
        <div><a t-att-href="route" target="_blank"><span class="fa fa-play ms-2"/></a></div>
    </div>`;

    static components = { Many2OneField }

    get route() {
        const model = this.props.relation || this.props.record.fields[this.props.name].relation;
        const id = this.props.value[0];
        if (model.startsWith('runbot.') ) {
            return '/runbot/' + model.split('.')[1] + '/' + this.props.record.resId;
        } else {
            return false;
        }
    }
}

registry.category("fields").add("frontend_url", FrontendUrl);


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

registry.category("fields").add("char_frontend_url", FieldCharFrontendUrl);

//export class GithubTeamWidget extends CharField {

//this.value.split(',').forEach((value) => {
//    const href = 'https://github.com/orgs/' + organisation + '/teams/' + value.trim() + '/members';
//}
//
//registry.category("fields").add("github_team", GithubTeamWidget);
