import { Component, onWillRender } from '@odoo/owl';

import { diff_match_patch } from "@runbot/libs/diff_match_patch/diff_match_patch";


export class DiffDisplay extends Component {
    static template = 'runbot.DiffDisplay';
    static props = {
        fromValue: { type: String },
        toValue: { type: String },
        lineFilter: { type: Function, optional: true },
    }
    static defaultProps = {
        lineFilter: (line) => line.type !== 'kept',
    }

    setup() {
        onWillRender(() => {
            this.lines = this.makeLines(this.props.fromValue, this.props.toValue);
        });
    }

    makeLines(oldValue, newValue) {
        const diff = this.makeDiff(oldValue, newValue);
        const lines = this.prepareForRendering(diff);
        return lines;
    }

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
    }

    prepareForRendering(diffs) {
        let preLineCounter = 0;
        let postLineCounter = 0;
        return diffs.reduce((lines, {0: diff_type, 1: data}) => {
            data.split('\n').forEach(line => {
                line = line
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;');
                let type, colOne, colTwo;
                switch (diff_type) {
                    case 0: //kept
                        type = 'kept'
                        colOne = ''
                        colTwo = postLineCounter;
                        preLineCounter++; postLineCounter++;
                        break;
                    case -1: //removed
                        type = 'removed';
                        colOne = preLineCounter;
                        colTwo = '-';
                        preLineCounter++;
                        break;
                    case 1: //added
                        type = 'added';
                        colOne = '+';
                        colTwo = postLineCounter;
                        postLineCounter++;
                        break;
                    default:
                        console.warn('Unknown diff_type', diff_type)
                        return;
                }
                lines.push({type, colOne, colTwo, line});
            })
            return lines
        }, []);
    }
}
