/* global Prism */
import { Component, onWillStart, xml } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Complete the official docker grammar with the shell syntax of RUN-like
 * instructions and the "-> FILE <destination>" header of runbot file layers.
 */
function extendDockerGrammar() {
    if (Prism.languages.docker.file) {
        return;
    }
    Prism.languages.insertBefore("inside", "operator", {
        flag: { pattern: /(\s)--?[a-zA-Z][\w-]*=?/, lookbehind: true },
        key: { pattern: /(\s)[A-Za-z_][\w.-]*=/, lookbehind: true },
        "shell-operator": /&&|\|\||[|;>]/,
    }, Prism.languages.docker.instruction);
    Prism.languages.insertBefore("docker", "comment", { file: /^-> FILE\b.*/m });
}

/**
 * Flatten Prism's token tree into lines of {type, text}, type being the leaf
 * token type ("" for plain text), nested tokens (instruction, options) only
 * wrap their content.
 */
function tokenizeDockerfile(content) {
    const lines = [[]];
    const visit = (token, type) => {
        if (typeof token === "string") {
            token.split("\n").forEach((text, index) => {
                if (index) {
                    lines.push([]);
                }
                if (text) {
                    lines.at(-1).push({ type, text });
                }
            });
        } else if (Array.isArray(token)) {
            token.forEach((child) => visit(child, type));
        } else {
            visit(token.content, typeof token.content === "string" ? token.type : type);
        }
    };
    visit(Prism.tokenize((content || "").replace(/\n$/, ""), Prism.languages.docker), "");
    return lines;
}

export class DockerfileField extends Component {
    static template = xml`
        <pre class="o_runbot_dockerfile"><t t-foreach="lines" t-as="line" t-key="line_index"><span class="o_runbot_dockerfile_line"><span t-foreach="line" t-as="token" t-key="token_index" t-att-class="token.type and 'o_runbot_dockerfile_' + token.type" t-out="token.text"/></span><t t-out="newline"/></t></pre>
    `;
    static props = { ...standardFieldProps };

    newline = "\n";

    setup() {
        onWillStart(async () => {
            await loadBundle("html_editor.assets_prism");
            extendDockerGrammar();
        });
    }

    get lines() {
        return tokenizeDockerfile(this.props.record.data[this.props.name]);
    }
}

registry.category("fields").add("dockerfile", {
    component: DockerfileField,
    supportedTypes: ["text"],
});
