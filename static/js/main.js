// Mobile Navigation Toggle
// Drives both the guest dropdown menu and the portal sidebar drawer:
// the same #navToggle / #navMenu hooks, plus a body.nav-open state that the
// drawer/backdrop CSS keys off.
const navToggle = document.getElementById('navToggle');
const navMenu = document.getElementById('navMenu');
const navBackdrop = document.getElementById('navBackdrop');

function setNavOpen(open) {
    document.body.classList.toggle('nav-open', open);
    if (navMenu) navMenu.classList.toggle('active', open);
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
}

if (navToggle) {
    navToggle.setAttribute('aria-expanded', 'false');
    navToggle.addEventListener('click', () => {
        setNavOpen(!document.body.classList.contains('nav-open'));
    });
}

// Close the menu / drawer when choosing a destination.
document.querySelectorAll('.nav-menu a').forEach(link => {
    link.addEventListener('click', () => {
        setNavOpen(false);
    });
});

// Close the drawer via its dimming backdrop or the Escape key.
if (navBackdrop) {
    navBackdrop.addEventListener('click', () => setNavOpen(false));
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.body.classList.contains('nav-open')) {
        setNavOpen(false);
        if (navToggle) navToggle.focus();
    }
});

// Reset the drawer state when returning to the desktop layout.
if (typeof window.matchMedia === 'function') {
    const desktopQuery = window.matchMedia('(min-width: 769px)');
    const resetOnDesktop = (event) => {
        if (event.matches) setNavOpen(false);
    };
    if (typeof desktopQuery.addEventListener === 'function') {
        desktopQuery.addEventListener('change', resetOnDesktop);
    } else if (typeof desktopQuery.addListener === 'function') {
        desktopQuery.addListener(resetOnDesktop);
    }
}

// Auto-hide alerts after 5 seconds
document.addEventListener('DOMContentLoaded', () => {
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.style.opacity = '0';
            alert.style.transition = 'opacity 0.5s ease';
            setTimeout(() => alert.remove(), 500);
        }, 5000);
    });
});

// Form validation
document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', (e) => {
        const requiredFields = form.querySelectorAll('[required]');
        let isValid = true;
        
        requiredFields.forEach(field => {
            if (!field.value.trim()) {
                isValid = false;
                field.style.borderColor = 'var(--danger-color)';
            } else {
                field.style.borderColor = 'var(--gray-200)';
            }
        });
        
        if (!isValid) {
            e.preventDefault();
            alert('Please fill in all required fields.');
        }
    });
});

// Smooth scroll
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
            target.scrollIntoView({
                behavior: 'smooth',
                block: 'start'
            });
        }
    });
});

// Add active class to current page nav item
const currentPage = window.location.pathname;
document.querySelectorAll('.nav-menu a').forEach(link => {
    if (link.getAttribute('href') === currentPage) {
        link.classList.add('active');
    }
});

// Light / dark theme toggle
const THEME_KEY = 'medicare-theme';
const themeToggle = document.getElementById('themeToggle');

function syncThemeToggle(theme) {
    if (!themeToggle) return;
    const nextLabel = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    themeToggle.setAttribute('aria-label', nextLabel);
    themeToggle.setAttribute('title', nextLabel);
    themeToggle.setAttribute('aria-pressed', String(theme === 'dark'));
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    syncThemeToggle(theme);
    try {
        localStorage.setItem(THEME_KEY, theme);
    } catch (e) {
        // Storage unavailable (private mode / disabled cookies) — theme still applies.
    }
    window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: theme } }));
}

if (themeToggle) {
    syncThemeToggle(document.documentElement.getAttribute('data-theme') || 'light');
    themeToggle.addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        applyTheme(current === 'dark' ? 'light' : 'dark');
    });
}

// Follow the OS preference until the visitor picks a theme explicitly.
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (event) => {
    let stored = null;
    try {
        stored = localStorage.getItem(THEME_KEY);
    } catch (e) {
        stored = null;
    }
    if (stored === 'dark' || stored === 'light') return;
    applyTheme(event.matches ? 'dark' : 'light');
});

// Show a 12-hour (AM/PM) preview beside native time inputs without changing
// their machine-readable 24-hour value.
(function () {
    function to12hr(value) {
        if (!value) return '';
        var parts = String(value).split(':');
        var hours = parseInt(parts[0], 10);
        var minutes = parseInt(parts[1], 10) || 0;
        if (isNaN(hours)) return '';
        var suffix = hours < 12 ? 'AM' : 'PM';
        var hour12 = hours % 12 || 12;
        return (hour12 < 10 ? '0' : '') + hour12 + ':' +
            (minutes < 10 ? '0' : '') + minutes + ' ' + suffix;
    }

    function attach(input) {
        if (input.dataset.time12Bound) return;
        input.dataset.time12Bound = '1';
        var hint = document.createElement('small');
        hint.className = 'time-12hr-hint';
        var update = function () {
            hint.textContent = to12hr(input.value);
        };
        input.addEventListener('input', update);
        input.addEventListener('change', update);
        input.parentNode.insertBefore(hint, input.nextSibling);
        update();
    }

    function init() {
        document.querySelectorAll('input[type="time"]').forEach(attach);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
