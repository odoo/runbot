import { Component, useEffect, useState } from '@odoo/owl';

import { registry } from '@web/core/registry';
import { useBus } from '@web/core/utils/hooks';
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { TextField } from '@web/views/fields/text/text_field';
import { X2ManyField, x2ManyField } from "@web/views/fields/x2many/x2many_field";
import { CheckBox } from "@web/core/checkbox/checkbox";

import { diff_match_patch } from "@runbot/libs/diff_match_patch/diff_match_patch";
import { DiffDisplay } from './diff_display';


export class ErrorContentOne2ManyList extends X2ManyField {
    static template = 'runbot.ErrorContentOne2ManyList';
    static components = {...X2ManyField.components, CheckBox};

    setup() {
        super.setup(...arguments);
        this.creates = [];
        this.state = useState({
            useDiff: localStorage.getItem('runbot.error_content_diff_mode') === 'true',
        });

        useEffect(() => {
            localStorage.setItem('runbot.error_content_diff_mode', this.state.useDiff);
            this.env.bus.trigger(
                'RUNBOT.TOGGLE-DIFF-MODE', {
                    mode: this.state.useDiff,
                },
            );
        }, () => [this.state.useDiff]);
    }

    get displayControlPanelButtons() {
        return true;
    }

    onToggle(state) {
        this.state.useDiff = state;
    }
}

export const errorContentOne2ManyList = {
    ...x2ManyField,
    component: ErrorContentOne2ManyList,
};

export class FieldErrorContentContent extends Component {
    static template = 'runbot.FieldErrorContentContent';
    static components = { TextField, DiffDisplay };
    static props = {...standardFieldProps};

    setup() {
        // We assume that the content is readonly here
        this.otherRecords = this.props.record.model.root.data.error_content_ids.records;
        this.state = useState({
            useDiff: localStorage.getItem('runbot.error_content_diff_mode') === 'true',
        });

        useBus(
            this.env.bus, 'RUNBOT.TOGGLE-DIFF-MODE',
            ({detail: {mode}}) => this.state.useDiff = mode,
        );
    }

    get isParent() {
        return this.otherRecords[0] === this.props.record;
    }

    get parent() {
        return this.otherRecords[0];
    }
}

export const fieldErrorContentContent = {
    component: FieldErrorContentContent,
    displayName: "Error Content diffable (list)",
    supportedTypes: ["html", "text", "char"],
};


registry.category('fields').add('error_content_list', errorContentOne2ManyList);
registry.category('fields').add('list.error_content_content', fieldErrorContentContent);
