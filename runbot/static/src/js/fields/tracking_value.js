/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { Message } from "@mail/core/common/message";

patch(Message.prototype, {
    setup() {
        super.setup(...arguments);
        this.kept = false;
    },
    isMultiline(trackingValue) {
        console.log(trackingValue)
        const oldValue = trackingValue.oldValue.value;
        const newValue = trackingValue.newValue.value;
        return ((oldValue && oldValue.includes('\n')) && (newValue && newValue.includes('\n')))
    },
    formatTracking(trackingType, trackingValue) {
        console.log('aaa')
        return super.formatTracking(trackingType, trackingValue) 
    },
    toggleKept() {
        this.kept = !this.kept;
    },
    copyOldToClipboard(trackingValue) {
        return function () {
            navigator.clipboard.writeText(trackingValue.oldValue.value);
        };
    },
    copyNewToClipboard(trackingValue) {
        return function () {
            navigator.clipboard.writeText(trackingValue.newValue.value);
        };
    },
    lines(trackingValue) {
        const oldValue = trackingValue.oldValue.value;
        const newValue = trackingValue.newValue.value;
        const diff = this.makeDiff(oldValue, newValue);
        const lines = this.prepareForRendering(diff);
        return lines;
    },
    makeDiff(text1, text2) {
        var dmp = new diff_match_patch();
        var a = dmp.diff_linesToChars_(text1, text2);
        var lineText1 = a.chars1;
        var lineText2 = a.chars2;
        var lineArray = a.lineArray;
        var diffs = dmp.diff_main(lineText1, lineText2, false);
        dmp.diff_charsToLines_(diffs, lineArray);
        dmp.diff_cleanupSemantic(diffs);
        return diffs;
    },
    prepareForRendering(diffs) {
        var lines = [];
        var pre_line_counter = 0
        var post_line_counter = 0
        for (var x = 0; x < diffs.length; x++) {
            var diff_type = diffs[x][0];
            var data = diffs[x][1];
            var data_lines = data.split('\n');
            for (var line_index in data_lines) {
                var line = data_lines[line_index];
                line = line.replace(/&/g, '&amp;');
                line = line.replace(/</g, '&lt;');
                line = line.replace(/>/g, '&gt;');
                //text = text.replace(/\n/g, '<br>');
                //text = text.replace(/ /g, '&nbsp&nbsp');
                if (diff_type == -1) {
                    lines.push({type:'removed', pre_line_counter: pre_line_counter, post_line_counter: '-', line: line})
                    pre_line_counter += 1
                } else if (diff_type == 0) {
                    lines.push({type:'kept', pre_line_counter: '', post_line_counter: post_line_counter, line: line})
                    pre_line_counter += 1
                    post_line_counter +=1
                } else if (diff_type == 1) {
                    lines.push({type:'added', pre_line_counter: '+', post_line_counter: post_line_counter, line: line})
                    post_line_counter +=1
                }
            }
        }
        return lines;
      },
});