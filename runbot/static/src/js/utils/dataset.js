/**
 * Extracts data-* attributes from an element and returns a plain object
 * with the "data-" prefix stripped from keys. Values are JSON-parsed
 * when possible (e.g. numbers, booleans, arrays, objects), otherwise
 * kept as raw strings.
 *
 * @param {HTMLElement} el - The element whose data-* attributes to extract.
 * @returns {Object<string, *>} An object mapping short attribute names
 *   (e.g. "build" from "data-build") to their parsed values.
 *
 * @example
 *   // <my-el data-id="42" data-name='"hello"' data-tags='["a","b"]'></my-el>
 *   extractDataset(myEl);
 *   // → { id: 42, name: "hello", tags: ["a", "b"] }
 */
export function extractDataset(el) {
    const dataset = {};
    for (const { name } of [...el.attributes]) {
        if (name.startsWith("data-")) {
            const rawValue = el.getAttribute(name);
            let value;
            try {
                value = JSON.parse(rawValue);
            } catch {
                value = rawValue;
            }
            dataset[name.slice(5)] = value;
        }
    }
    return dataset;
}
