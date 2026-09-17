import { preferences } from "../utils/preferences";

class UserPreference extends HTMLElement {
    constructor() {
        super();
        // listened to on the form and its buttons, not on the element itself
        this.onChange = this.onChange.bind(this);
        this.onSubmit = this.onSubmit.bind(this);
        this.onPreset = this.onPreset.bind(this);
    }

    connectedCallback() {
        this.form = this.querySelector("form");
        if (!this.form) {
            throw new Error("The controls of a preference must be held in a form");
        }
        this.form.addEventListener("change", this.onChange);
        this.form.addEventListener("submit", this.onSubmit);
        for (const button of this.buttons) {
            button.addEventListener("click", this.onPreset);
        }
    }

    disconnectedCallback() {
        this.form.removeEventListener("change", this.onChange);
        this.form.removeEventListener("submit", this.onSubmit);
        for (const button of this.buttons) {
            button.removeEventListener("click", this.onPreset);
        }
    }

    get controls() {
        return [...this.form.elements].filter((el) => ["checkbox", "radio"].includes(el.type));
    }

    get buttons() {
        return [...this.form.elements].filter((el) => el.type === "button");
    }

    get name() {
        return this.controls[0]?.name;
    }

    get value() {
        return new FormData(this.form).getAll(this.name).sort().join("-");
    }

    get defaultValue() {
        return (this.dataset.default ?? "").split("-").sort().join("-");
    }

    save() {
        const { name, value } = this;
        if (value === this.defaultValue) {
            preferences.remove(name);
        } else {
            preferences.set(name, value);
        }
    }

    onChange() {
        if (![...this.form.elements].some((el) => el.type === "submit")) {
            this.form.requestSubmit();
        }
    }

    onSubmit(event) {
        event.preventDefault();
        this.save();
        window.location.reload();
    }

    onPreset(event) {
        const preset = event.target.value;
        const defaults = this.defaultValue.split("-");
        for (const control of this.controls) {
            control.checked =
                preset === "all" || (preset === "default" && defaults.includes(control.value));
        }
    }
}

customElements.define("user-preference", UserPreference);
