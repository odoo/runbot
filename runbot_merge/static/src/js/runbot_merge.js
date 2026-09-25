/* region light/dark selector */
function setColorScheme(t) {
    const classes = document.documentElement.classList;
    classes.remove('light', 'dark');

    const buttons = document.querySelectorAll('.theme-toggle button');
    for(const button of buttons) {
        button.classList.toggle(
            'active',
            (t === 'light' && button.classList.contains('fa-sun-o'))
            || (t === 'dark' && button.classList.contains('fa-moon-o'))
            || (t !== 'light' && t !== 'dark' && button.classList.contains('fa-ban'))
        );
    }

    switch (t) {
    case 'light': case 'dark':
        classes.add(t);
        window.localStorage.setItem('color-scheme', t);
        break;
    default:
        window.localStorage.removeItem('color-scheme');
    }
}

window.addEventListener("click", (e) => {
    const target = e.target;
    if (target.matches(".theme-toggle button")) {
        setColorScheme(
            target.classList.contains('fa-sun-o') ? 'light' :
                target.classList.contains('fa-moon-o') ? 'dark' :
                    null
        );
    }
});

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", (e) => {
        setColorScheme(window.localStorage.getItem('color-scheme'));
    });
} else {
    setColorScheme(window.localStorage.getItem('color-scheme'));
}

/* endregion */

/* region cross-staging batch highlighting */
window.addEventListener("mouseover", (e) => {
    const batch = e.target.closest('[data-batch-id]');
    if (!batch) return;
    // Only trigger if coming from outside this batch
    const related = e.relatedTarget;
    if (!related || !batch.contains(related)) {
        for (const b of document.querySelectorAll(`[data-batch-id="${batch.dataset.batchId}"]`)) {
            b.style.outline = 'thin dashed';
        }
    }
});
window.addEventListener("mouseout", (e) => {
    const batch = e.target.closest('[data-batch-id]');
    if (!batch) return;
    // Only trigger if leaving to outside this batch
    const related = e.relatedTarget;
    if (!related || !batch.contains(related)) {
        for (const b of document.querySelectorAll(`[data-batch-id="${batch.dataset.batchId}"]`)) {
            b.style.outline = '';
        }
    }
});
/* endregion */
window.addEventListener("click", e => {
    const toggle = e.target.closest('a.dropdown-toggle');
    if (toggle) {
        toggle.classList.toggle('show');
    } else {
        const openToggle = document.querySelector('.dropdown-toggle.show');
        if (openToggle) {
            openToggle.classList.remove('show');
        }
    }
});

window.addEventListener("click", e => {
    const title = e.target.tagName !== 'A' && e.target.closest('section>section>h2');
    if (title) {
        title.classList.toggle('fold');
    }
});
