// landing.js — loaded via <script type="module" src="js/landing.js">
// Modules are deferred by default and strict-mode by default.

// --- Navbar Scroll Transition ---
function initNavbar() {
    const navbar = document.getElementById('navbar');
    if (!navbar) return;

    let ticking = false;

    function onScroll() {
        if (!ticking) {
            requestAnimationFrame(() => {
                if (window.scrollY > 50) {
                    navbar.classList.add('lp-navbar--scrolled');
                } else {
                    navbar.classList.remove('lp-navbar--scrolled');
                }
                ticking = false;
            });
            ticking = true;
        }
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
}


// --- Smooth Scroll for Anchor Links ---
function initSmoothScroll() {
    document.querySelectorAll('a[href^="#"]').forEach(link => {
        link.addEventListener('click', (e) => {
            const href = link.getAttribute('href');
            if (href === '#') return;

            const target = document.querySelector(href);
            if (!target) return;

            e.preventDefault();
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });
}


// --- Mobile Navigation Toggle ---
function initMobileNav() {
    const toggle = document.getElementById('nav-toggle');
    const navLinks = document.getElementById('nav-links');
    if (!toggle || !navLinks) return;

    function closeMenu() {
        navLinks.classList.remove('lp-mobile-nav--open');
        toggle.setAttribute('aria-expanded', 'false');
    }

    function openMenu() {
        navLinks.classList.add('lp-mobile-nav--open');
        toggle.setAttribute('aria-expanded', 'true');
    }

    toggle.addEventListener('click', () => {
        const isOpen = navLinks.classList.contains('lp-mobile-nav--open');
        if (isOpen) {
            closeMenu();
        } else {
            openMenu();
        }
    });

    // Close when a nav link is clicked
    navLinks.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', closeMenu);
    });

    // Close when clicking outside
    document.addEventListener('click', (e) => {
        if (!navLinks.contains(e.target) && !toggle.contains(e.target)) {
            closeMenu();
        }
    });

    // Close on Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeMenu();
        }
    });
}


// --- FAQ Accordion ---
function initFAQ() {
    const items = document.querySelectorAll('.lp-faq__item');

    items.forEach(item => {
        const button = item.querySelector('.lp-faq__question');
        const answer = item.querySelector('.lp-faq__answer');
        if (!button || !answer) return;

        button.addEventListener('click', () => {
            const isOpen = button.getAttribute('aria-expanded') === 'true';

            // Close all other items
            items.forEach(otherItem => {
                const otherButton = otherItem.querySelector('.lp-faq__question');
                const otherAnswer = otherItem.querySelector('.lp-faq__answer');
                if (otherButton && otherAnswer && otherItem !== item) {
                    otherButton.setAttribute('aria-expanded', 'false');
                    otherAnswer.style.maxHeight = null;
                }
            });

            // Toggle current
            if (isOpen) {
                button.setAttribute('aria-expanded', 'false');
                answer.style.maxHeight = null;
            } else {
                button.setAttribute('aria-expanded', 'true');
                answer.style.maxHeight = answer.scrollHeight + 'px';
            }
        });
    });
}


// --- Scroll-Triggered Fade-In Animations ---
function initScrollAnimations() {
    const elements = document.querySelectorAll('.lp-animate');
    if (!elements.length) return;

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('lp-animate--visible');
                observer.unobserve(entry.target);
            }
        });
    }, {
        threshold: 0.1,
        rootMargin: '0px 0px -40px 0px'
    });

    elements.forEach(el => observer.observe(el));
}


// --- Waitlist Form Handler ---
function initWaitlistForm() {
    const form = document.querySelector('.lp-waitlist-form');
    if (!form) return;

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const input = form.querySelector('.lp-waitlist-form__input');
        const btn = form.querySelector('.lp-waitlist-form__btn');
        btn.textContent = 'You\'re on the list!';
        btn.disabled = true;
        input.disabled = true;
        input.value = '';
    });
}


// --- Initialize ---
initNavbar();
initSmoothScroll();
initMobileNav();
initFAQ();
initScrollAnimations();
initWaitlistForm();
