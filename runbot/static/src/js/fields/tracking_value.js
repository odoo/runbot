/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { Message } from "@mail/core/common/message";
import { CopyButton } from "@web/core/copy_button/copy_button";
import { memoize } from "@web/core/utils/functions";

const diffMatchPatch = new diff_match_patch();

function makeDiff(text1, text2) {
    const { chars1, chars2, lineArray } = diffMatchPatch.diff_linesToChars_(text1, text2);
    const diffs = diffMatchPatch.diff_main(chars1, chars2, false);
    diffMatchPatch.diff_charsToLines_(diffs, lineArray);
    diffMatchPatch.diff_cleanupSemantic(diffs);
    return diffs;
}

function prepareForRendering(diffs) {
    const lines = [];
    let preLineCounter = 0;
    let postLineCounter = 0;
    for (const { 0: diffType, 1: data } of diffs) {
        for (let line of data.split("\n")) {
            line = line.replace(/&/g, "&amp;");
            line = line.replace(/</g, "&lt;");
            line = line.replace(/>/g, "&gt;");
            //text = text.replace(/\n/g, "<br>");
            //text = text.replace(/ /g, "&nbsp&nbsp");
            let type;
            if (diffType === -1) {
                type = "removed";
                preLineCounter += 1;
            } else if (diffType === 0) {
                type = "kept";
                preLineCounter += 1;
                postLineCounter += 1;
            } else if (diffType === 1) {
                type = "added";
                postLineCounter += 1;
            }
            lines.push({ type, preLineCounter, postLineCounter, line });
        }
    }
    return lines;
}

function computeDiff({ oldValue, newValue }) {
    const diff = makeDiff(oldValue, newValue);
    return prepareForRendering(diff);
}

patch(Message, {
    components: {
        ...Message.components,
        CopyButton,
    },
});

patch(Message.prototype, {
    setup() {
        super.setup(...arguments);
        this.state.showKept = false;
    },

    lines: memoize(computeDiff),

    isMultiline({ oldValue, newValue }) {
        return (
            typeof oldValue === "string" &&
            oldValue.includes("\n") &&
            typeof oldValue === "string" &&
            newValue.includes("\n")
        );
    },

    toggleKept() {
        this.state.showKept = !this.state.showKept;
    },
});
