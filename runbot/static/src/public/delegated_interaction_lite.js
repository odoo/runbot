export class DelegatedInteractionLite {
    static delegatedEvents = ["click"];
    static selector = null;

    dynamicContent = {};
    readyResolvers = Promise.withResolvers();

    constructor() {
        document.addEventListener("DOMContentLoaded", this.onDOMContentLoaded.bind(this));
    }

    /**
     * @returns {Promise}
     */
    get whenReady() {
        return this.readyResolvers.promise;
    }

    /**
     * @returns {Array}
     */
    get dynamicContentDescriptors() {
        const descriptors = [];
        for (const [selector, attrs] of Object.entries(this.dynamicContent)) {
            for (const [key, fn] of Object.entries(attrs)) {
                const [eventKey, ...flags] = key.split(".");
                const eventName = eventKey.replace("t-on-", "");
                const desc = { selector, eventName, flags, fn };
                descriptors.push(desc);
            }
        }
        return descriptors;
    }

    /**
     * @param {string} eventName
     * @returns {Function}
     */
    delegatedEventListener(eventName) {
        return (ev) => {
            for (const desc of this.dynamicContentDescriptors) {
                if (desc.eventName !== eventName) {
                    continue;
                }
                const delegatedTarget = ev.target.matches(desc.selector) ? ev.target : ev.target.closest(desc.selector);
                if (!delegatedTarget) {
                    continue;
                }
                console.debug("delegatedEventListener", eventName, desc, delegatedTarget);
                Object.assign(ev, { delegatedTarget });
                if (desc.flags.includes("prevent")) {
                    ev.preventDefault();
                }
                if (desc.flags.includes("stop")) {
                    ev.stopPropagation();
                }
                let { fn } = desc;
                if (desc.flags.includes("bind")) {
                    fn = fn.bind(this);
                }
                fn(ev);
            }
        };
    }

    /**
     * @returns {void}
     */
    onDOMContentLoaded() {
        console.debug("onDOMContentLoaded");
        this.el = this.constructor.selector ? document.querySelector(this.constructor.selector) : document.body;
        if (!this.el) {
            console.debug("onDOMContentLoaded: ", this.constructor.selector, "not found");
            return;
        }
        for (const eventName of this.constructor.delegatedEvents) {
            this.el.addEventListener(eventName, this.delegatedEventListener(eventName));
        }
        this.readyResolvers.resolve();
    }
}
