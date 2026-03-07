/**
 * Sidebar navigation renderer.
 * Call renderNav() on each app page to inject the sidebar.
 */
import { signOut, getUserEmail } from "./auth.js";
import { escapeHtml } from "./ui.js";

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
        label: "History",
        href: "/app/history.html",
        icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>`,
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

    const links = NAV_ITEMS.map(item => {
        const isActive = currentPath.endsWith(item.href) || currentPath.endsWith(item.href.replace("/app/", ""));
        return `<a href="${item.href}" class="em-sidebar-link${isActive ? " active" : ""}">${item.icon}<span>${item.label}</span></a>`;
    }).join("");

    sidebar.innerHTML = `
        <div class="em-sidebar-brand">
            <div class="em-sidebar-brand-icon">CA</div>
            <span>Clarion AI</span>
        </div>
        <div class="em-sidebar-nav">${links}</div>
        <div class="em-sidebar-footer">
            <div class="em-sidebar-avatar">${initial}</div>
            <div class="em-sidebar-user-info">
                <div class="em-sidebar-email">${escapeHtml(email || "\u2014")}</div>
                <button class="em-sidebar-logout" id="em-logout-btn">Log out</button>
            </div>
        </div>
    `;

    document.getElementById("em-logout-btn").addEventListener("click", signOut);
}
