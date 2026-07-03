import { evaluateExpr } from "@web/core/py_js/py";

/**
 * Interpolates `{{ expression }}` occurrences in a string.
 *
 * @param {string} value
 * @param {Object} scope
 * @returns {string}
 */
function interpolate(value, scope) {
    if (!value.includes("{{")) {
        return value;
    }
    return value.replace(
        /\{\{(.*?)\}\}/gs,
        (_, expression) => evaluateExpr(expression.trim(), scope) ?? "",
    );
}

/**
 * Processes the direct children of a DOM node.
 *
 * @param {Node} parent
 * @param {Object} scope
 */
function processChildren(parent, scope) {
    for (const node of [...parent.childNodes]) {
        if (node.parentNode === parent) {
            processNode(node, scope);
        }
    }
}

/**
 * Processes a template node and its descendants.
 *
 * @param {Node} node
 * @param {Object} scope
 */
function processNode(node, scope) {
    if (node.nodeType === Node.TEXT_NODE) {
        node.textContent = interpolate(node.textContent, scope);
        return;
    }

    if (node.nodeType !== Node.ELEMENT_NODE) {
        return;
    }

    const foreachExpression = node.getAttribute("t-foreach");

    if (foreachExpression !== null) {
        const items = evaluateExpr(foreachExpression, scope) || [];

        if (
            typeof items === "string" ||
            typeof items[Symbol.iterator] !== "function"
        ) {
            throw new TypeError(
                `t-foreach="${foreachExpression}" must evaluate to a non-string iterable`,
            );
        }

        const name = node.getAttribute("t-as") || "item";

        for (const item of items) {
            const clone = node.cloneNode(true);
            clone.removeAttribute("t-foreach");
            clone.removeAttribute("t-as");
            node.before(clone);
            processNode(clone, { ...scope, [name]: item });
        }

        node.remove();
        return;
    }

    const ifExpression = node.getAttribute("t-if");

    if (ifExpression !== null) {
        const alternative = node.nextElementSibling?.hasAttribute("t-else")
            ? node.nextElementSibling
            : null;

        if (evaluateExpr(ifExpression, scope)) {
            node.removeAttribute("t-if");
            alternative?.remove();
        } else {
            node.remove();
            alternative?.removeAttribute("t-else");
            return;
        }
    } else if (node.hasAttribute("t-else")) {
        node.remove();
        return;
    }

    for (const attribute of node.attributes) {
        attribute.value = interpolate(attribute.value, scope);
    }

    processChildren(node, scope);
}

/**
 * Renders a DOM template fragment using a minimal QWeb-like syntax.
 *
 * Supported features:
 * - `t-if` and `t-else` conditional rendering
 * - `t-foreach` and `t-as` iteration
 * - `{{ expression }}` interpolation in text nodes and attribute values
 *
 * A `t-else` element must be the next element sibling of its corresponding
 * `t-if` element. Whitespace and other text nodes between them are ignored.
 *
 * Falsy `t-foreach` values are treated as empty collections. Truthy values
 * must be non-string iterables or a `TypeError` is raised.
 *
 * @param {DocumentFragment} template
 *   The template fragment to render.
 * @param {Object} [context={}]
 *   The initial scope available to template expressions.
 * @returns {DocumentFragment}
 *   A rendered clone of the template fragment.
 */
export function render(template, context = {}) {
    const result = template.cloneNode(true);
    processChildren(result, context);
    return result;
}
