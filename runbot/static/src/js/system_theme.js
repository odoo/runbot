// @odoo-module ignore
(function () {
    const scheme = matchMedia("(prefers-color-scheme: dark)");
    const setColorTheme = () => (document.documentElement.dataset.bsTheme = scheme.matches ? "dark" : "light");
    setColorTheme();
    scheme.addEventListener("change", setColorTheme);
})();
