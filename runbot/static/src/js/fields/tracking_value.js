import { patch } from "@web/core/utils/patch";
import { Message } from "@mail/core/common/message";

patch(Message.prototype, {
    setup() {
        super.setup(...arguments);
        this.kept = false;
    },

    isMultiline(trackingValue) {
        const oldValue = trackingValue.oldValue;
        const newValue = trackingValue.newValue;
        return ((oldValue && typeof oldValue=== "string" && oldValue.includes("\n")) && (newValue && typeof oldValue=== "string" && newValue.includes("\n")));
    },

    formatTracking(trackingFieldInfo, trackingValue) {
        return super.formatTracking(trackingFieldInfo, trackingValue);
    },

    toggleKept() {
        this.kept = !this.kept;
    },

    copyToClipboard(trackingValue) {
        return function () {
            navigator.clipboard.writeText(trackingValue);
        };
    },

    lines(trackingValue) {
        const oldValue = trackingValue.oldValue;
        const newValue = trackingValue.newValue;
        const diff = this.makeDiff(oldValue, newValue);
        const lines = this.prepareForRendering(diff);
        return lines;
    },

    makeDiff(text1, text2) {
        const dmp = new diff_match_patch();
        const a = dmp.diff_linesToChars_(text1, text2);
        const lineText1 = a.chars1;
        const lineText2 = a.chars2;
        const lineArray = a.lineArray;
        const diffs = dmp.diff_main(lineText1, lineText2, false);
        dmp.diff_charsToLines_(diffs, lineArray);
        dmp.diff_cleanupSemantic(diffs);
        return diffs;
    },

    prepareForRendering(diffs) {
        const lines = [];
        let pre_line_counter = 0;
        let post_line_counter = 0;
        for (let x = 0; x < diffs.length; x++) {
            const diff_type = diffs[x][0];
            const data = diffs[x][1];
            const data_lines = data.split("\n");
            for (const line_index in data_lines) {
                let line = data_lines[line_index];
                line = line.replace(/&/g, "&amp;");
                line = line.replace(/</g, "&lt;");
                line = line.replace(/>/g, "&gt;");
                //text = text.replace(/\n/g, '<br>');
                //text = text.replace(/ /g, '&nbsp&nbsp');
                if (diff_type == -1) {
                    lines.push({ type: "removed", pre_line_counter: pre_line_counter, post_line_counter: "-", line: line });
                    pre_line_counter += 1;
                } else if (diff_type == 0) {
                    lines.push({ type: "kept", pre_line_counter: "", post_line_counter: post_line_counter, line: line });
                    pre_line_counter += 1;
                    post_line_counter += 1;
                } else if (diff_type == 1) {
                    lines.push({ type: "added", pre_line_counter: "+", post_line_counter: post_line_counter, line: line });
                    post_line_counter += 1;
                }
            }
        }
        return lines;
    },
});
