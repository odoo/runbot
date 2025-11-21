import { patch } from "@web/core/utils/patch";
import { CodeEditor } from "@web/core/code_editor/code_editor";

patch(CodeEditor, {
    MODES: [...new Set(CodeEditor.MODES).add("json")],
});
