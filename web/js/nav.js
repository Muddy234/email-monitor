/**
 * Sidebar navigation renderer.
 * Call renderNav() on each app page to inject the sidebar.
 */
import { signOut, getUserEmail } from "./auth.js";
import { escapeHtml } from "./ui.js";

const DEV_MODE_KEY = "clarion_dev_mode";

export function isDevMode() {
    return localStorage.getItem(DEV_MODE_KEY) === "true";
}

export function setDevMode(enabled) {
    localStorage.setItem(DEV_MODE_KEY, enabled ? "true" : "false");
}

const NAV_ITEMS = [
    {
        label: "Dashboard",
        href: "/app/dashboard.html",
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" /></svg>`,
    },
    {
        label: "Emails",
        href: "/app/emails.html",
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" /></svg>`,
    },
    {
        label: "Contacts",
        href: "/app/contacts.html",
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" /></svg>`,
    },
    {
        label: "Analytics",
        href: "/app/analytics.html",
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg>`,
    },
    {
        label: "History",
        href: "/app/history.html",
        devOnly: true,
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>`,
    },
    {
        label: "Dev Tools",
        href: "/app/devtools.html",
        devOnly: true,
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M11.42 15.17l-5.01-5.01m0 0l5.01-5.01m-5.01 5.01H21.75M3.75 3v18" /></svg>`,
    },
];

/**
 * Render the sidebar into the page.
 * Expects a <nav id="em-sidebar"></nav> element in the HTML.
 */
export async function renderNav() {
    const sidebar = document.getElementById("em-sidebar");
    if (!sidebar) return;

    const currentPath = window.location.pathname;
    const email = await getUserEmail();
    const initial = (email || "?")[0].toUpperCase();
    const devMode = isDevMode();

    const visibleItems = NAV_ITEMS.filter(item => !item.devOnly || devMode);

    const links = visibleItems.map(item => {
        const isActive = currentPath.endsWith(item.href) || currentPath.endsWith(item.href.replace("/app/", ""));
        return `<a href="${item.href}" class="em-sidebar-link${isActive ? " active" : ""}">${item.icon}<span>${item.label}</span></a>`;
    }).join("");

    sidebar.innerHTML = `
        <div class="em-sidebar-brand">
            <div class="em-sidebar-brand-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none" width="24" height="24"><rect x="2" y="6" width="28" height="20" rx="3" stroke="currentColor" stroke-width="2.2"/><path d="M2 9l13.1 8.3a1.94 1.94 0 0 0 1.8 0L30 9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="26" cy="8" r="4" fill="#10B981"/><circle cx="26" cy="8" r="6" fill="none" stroke="#10B981" stroke-width="1.2" opacity="0.5"/></svg></div>
            <span>Clarion AI</span>
        </div>
        <div class="em-sidebar-nav">${links}</div>
        <div class="em-sidebar-footer">
            <div class="em-sidebar-avatar">${initial}</div>
            <div class="em-sidebar-user-info">
                <div class="em-sidebar-email">${escapeHtml(email || "\u2014")}</div>
                <div class="em-sidebar-actions">
                    <label class="em-dev-toggle" title="Show developer tools">
                        <input type="checkbox" id="em-dev-mode-toggle" ${devMode ? "checked" : ""}>
                        <span>Dev</span>
                    </label>
                    <button class="em-sidebar-logout" id="em-logout-btn">Log out</button>
                </div>
            </div>
        </div>
    `;

    document.getElementById("em-logout-btn").addEventListener("click", signOut);

    document.getElementById("em-dev-mode-toggle").addEventListener("change", (e) => {
        setDevMode(e.target.checked);
        renderNav();
    });
}
