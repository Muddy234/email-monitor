/**
 * Shared email picker component for Dev Tools panels.
 * Fetches emails once and caches them. Provides search/select UI.
 */
import { supabase } from "../supabase-client.js";
import { escapeHtml, formatDate } from "../ui.js";

let cachedEmails = null;

export function clearEmailCache() {
    cachedEmails = null;
}

export async function getEmails() {
    if (cachedEmails) return cachedEmails;

    const { data: { user } } = await supabase.auth.getUser();
    if (!user) return [];

    const { data, error } = await supabase
        .from("emails")
        .select("id, subject, sender_email, sender_name, received_time, body, status, classifications(*), drafts(*)")
        .eq("user_id", user.id)
        .order("received_time", { ascending: false })
        .limit(500);

    if (error) {
        console.error("email-picker: fetch failed", error);
        return [];
    }
    cachedEmails = data || [];
    return cachedEmails;
}

export function createEmailPicker(container, onSelect) {
    container.innerHTML = `
        <input type="text" class="em-picker-search" placeholder="Search by sender or subject..." />
        <div class="em-picker-list"></div>
    `;

    const searchInput = container.querySelector(".em-picker-search");
    const listEl = container.querySelector(".em-picker-list");
    let allEmails = [];
    let selectedId = null;

    function render(emails) {
        if (!emails.length) {
            listEl.innerHTML = `<div class="em-empty" style="padding:20px">No emails found.</div>`;
            return;
        }
        const shown = emails.slice(0, 100);
        listEl.innerHTML = shown.map(e => {
            const cls = e.classifications?.[0];
            const hasDraft = e.drafts?.length > 0;
            const isSelected = e.id === selectedId;
            return `<div class="em-picker-item${isSelected ? " selected" : ""}" data-id="${e.id}">
                <div class="em-picker-sender">${escapeHtml(e.sender_name || e.sender_email || "Unknown")}</div>
                <div class="em-picker-subject">${escapeHtml(e.subject || "(no subject)")}</div>
                <div class="em-picker-meta">
                    <span>${formatDate(e.received_time)}</span>
                    ${cls?.needs_response ? '<span class="em-badge em-badge-amber" style="font-size:10px">Needs Response</span>' : ""}
                    ${hasDraft ? '<span class="em-badge em-badge-green" style="font-size:10px">Has Draft</span>' : ""}
                </div>
            </div>`;
        }).join("");
    }

    function filter(query) {
        if (!query) return allEmails;
        const q = query.toLowerCase();
        return allEmails.filter(e =>
            (e.sender_email || "").toLowerCase().includes(q) ||
            (e.sender_name || "").toLowerCase().includes(q) ||
            (e.subject || "").toLowerCase().includes(q)
        );
    }

    listEl.addEventListener("click", (evt) => {
        const item = evt.target.closest(".em-picker-item");
        if (!item) return;
        selectedId = item.dataset.id;
        const email = allEmails.find(e => e.id === selectedId);
        render(filter(searchInput.value));
        if (email) onSelect(email);
    });

    let debounceTimer;
    searchInput.addEventListener("input", () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => render(filter(searchInput.value)), 200);
    });

    // Load
    getEmails().then(emails => {
        allEmails = emails;
        render(allEmails);
    });
}
