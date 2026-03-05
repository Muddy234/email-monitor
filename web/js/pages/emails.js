/**
 * Emails page — grouped list with inline detail, draft editing, mark completed.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, getParam, setParam, formatDate } from "../ui.js";

await requireAuth();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

const PAGE_SIZE = 50;
let allEmails = [];
let totalCount = 0;
let page = parseInt(getParam("page", "1"), 10);
let searchQuery = getParam("q", "");

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const container = document.getElementById("emailsContainer");
const paginationEl = document.getElementById("pagination");
const searchInput = document.getElementById("searchInput");
const refreshBtn = document.getElementById("refreshBtn");

searchInput.value = searchQuery;

// -------------------------------------------------------------------------
// Search
// -------------------------------------------------------------------------

let searchTimer = null;
searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        searchQuery = searchInput.value.trim().toLowerCase();
        setParam("q", searchQuery);
        renderEmails();
    }, 300);
});

refreshBtn.addEventListener("click", () => {
    page = 1;
    setParam("page", "");
    loadEmails();
});

// -------------------------------------------------------------------------
// Load emails from Supabase
// -------------------------------------------------------------------------

async function loadEmails() {
    try {
        const offset = (page - 1) * PAGE_SIZE;
        const { data, error, count } = await supabase
            .from("emails")
            .select("*, classifications(*), drafts(*)", { count: "exact" })
            .order("received_time", { ascending: false })
            .range(offset, offset + PAGE_SIZE - 1);

        if (error) throw error;

        allEmails = data || [];
        totalCount = count || 0;
        renderEmails();
        renderPagination();
    } catch (err) {
        showError(`Failed to load emails: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Group and render emails
// -------------------------------------------------------------------------

function filterEmails() {
    if (!searchQuery) return allEmails;
    return allEmails.filter(e =>
        (e.sender || "").toLowerCase().includes(searchQuery) ||
        (e.sender_name || "").toLowerCase().includes(searchQuery) ||
        (e.subject || "").toLowerCase().includes(searchQuery)
    );
}

function groupEmails(emails) {
    const groups = {
        "Drafts Ready": [],
        "Needs Response": [],
        "Other": [],
        "Completed": [],
    };

    for (const email of emails) {
        const hasDraft = email.drafts && email.drafts.length > 0;
        const needsResponse = email.classifications?.some(c => c.needs_response);

        if (email.status === "completed") {
            groups["Completed"].push(email);
        } else if (hasDraft) {
            groups["Drafts Ready"].push(email);
        } else if (needsResponse) {
            groups["Needs Response"].push(email);
        } else {
            groups["Other"].push(email);
        }
    }

    return groups;
}

function renderEmails() {
    const filtered = filterEmails();

    if (filtered.length === 0) {
        showEmpty(container, allEmails.length === 0
            ? "No emails synced yet."
            : "No emails match your search.");
        return;
    }

    const groups = groupEmails(filtered);
    let html = "";

    for (const [groupName, emails] of Object.entries(groups)) {
        if (emails.length === 0) continue;

        html += `<div class="em-group">`;
        html += `<div class="em-group-title">${groupName}<span class="em-group-count">${emails.length}</span></div>`;
        html += `<div class="em-email-list">`;

        for (const email of emails) {
            const cls = email.classifications?.[0];
            const draft = email.drafts?.[0];
            const actionSnippet = cls?.action ? cls.action.substring(0, 100) : "";
            const priorityBadge = cls?.priority >= 3
                ? `<span class="em-badge em-badge-red">P${cls.priority}</span>`
                : cls?.priority >= 2
                ? `<span class="em-badge em-badge-amber">P${cls.priority}</span>`
                : "";

            html += `
                <div class="em-email-card" data-id="${email.id}">
                    <div class="em-email-header">
                        <div>
                            <div class="em-email-sender">${escapeHtml(email.sender_name || email.sender || "Unknown")}</div>
                            <div class="em-email-subject">${escapeHtml(email.subject || "(no subject)")}</div>
                            ${actionSnippet ? `<div class="em-email-action">${escapeHtml(actionSnippet)}</div>` : ""}
                        </div>
                        <div style="text-align: right; flex-shrink: 0;">
                            <div class="em-email-meta">${formatDate(email.received_time)} ${priorityBadge}</div>
                            ${draft ? `<span class="em-badge em-badge-blue" style="margin-top: 4px;">Draft</span>` : ""}
                            ${email.status === "completed" ? `<span class="em-badge em-badge-green" style="margin-top: 4px;">Done</span>` : ""}
                        </div>
                    </div>

                    <div class="em-email-detail">
                        <div class="em-email-section-label">Email Body</div>
                        <div class="em-email-body">${escapeHtml(email.body || "No body available.")}</div>

                        ${cls ? `
                            <div class="em-email-section-label">Classification</div>
                            <div style="font-size: 13px; color: var(--em-slate-700); margin-bottom: 16px;">
                                <div><strong>Action:</strong> ${escapeHtml(cls.action || "\u2014")}</div>
                                <div><strong>Context:</strong> ${escapeHtml(cls.context || "\u2014")}</div>
                                <div><strong>Project:</strong> ${escapeHtml(cls.project || "\u2014")}</div>
                                <div><strong>Needs Response:</strong> ${cls.needs_response ? "Yes" : "No"}</div>
                            </div>
                        ` : ""}

                        ${draft ? `
                            <div class="em-email-section-label">Draft Response</div>
                            <div class="em-draft-editor">
                                <textarea class="em-draft-textarea" data-draft-id="${draft.id}">${escapeHtml(draft.draft_body || "")}</textarea>
                                <div class="em-draft-actions">
                                    <button class="em-btn em-btn-primary em-btn-sm draft-save-btn" data-draft-id="${draft.id}">Save</button>
                                    <span class="em-saved-msg" data-saved-for="${draft.id}">Saved</span>
                                </div>
                            </div>
                        ` : ""}

                        ${email.status !== "completed" ? `
                            <div style="margin-top: 12px;">
                                <button class="em-btn em-btn-success em-btn-sm mark-completed-btn" data-email-id="${email.id}">Mark Completed</button>
                            </div>
                        ` : ""}
                    </div>
                </div>
            `;
        }

        html += `</div></div>`;
    }

    container.innerHTML = html;
    bindCardEvents();
}

// -------------------------------------------------------------------------
// Pagination
// -------------------------------------------------------------------------

function renderPagination() {
    if (totalCount <= PAGE_SIZE && page === 1) {
        paginationEl.innerHTML = "";
        return;
    }

    const showing = Math.min(page * PAGE_SIZE, totalCount);
    paginationEl.innerHTML = `
        <span>Showing ${showing} of ${totalCount} emails</span>
        ${totalCount > page * PAGE_SIZE
            ? `<button class="em-btn em-btn-secondary em-btn-sm" id="loadMoreBtn">Load More</button>`
            : ""}
    `;

    const loadMoreBtn = document.getElementById("loadMoreBtn");
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener("click", async () => {
            page++;
            setParam("page", String(page));
            const offset = (page - 1) * PAGE_SIZE;
            try {
                const { data, error } = await supabase
                    .from("emails")
                    .select("*, classifications(*), drafts(*)")
                    .order("received_time", { ascending: false })
                    .range(offset, offset + PAGE_SIZE - 1);

                if (error) throw error;
                allEmails = allEmails.concat(data || []);
                renderEmails();
                renderPagination();
            } catch (err) {
                showError(`Failed to load more: ${err.message}`);
            }
        });
    }
}

// -------------------------------------------------------------------------
// Card interactions
// -------------------------------------------------------------------------

function bindCardEvents() {
    // Expand/collapse
    document.querySelectorAll(".em-email-card").forEach(card => {
        card.addEventListener("click", (e) => {
            // Don't toggle if clicking buttons or textarea
            if (e.target.closest("button") || e.target.closest("textarea")) return;
            card.classList.toggle("expanded");
        });
    });

    // Auto-resize textareas
    document.querySelectorAll(".em-draft-textarea").forEach(ta => {
        ta.addEventListener("input", () => {
            ta.style.height = "auto";
            ta.style.height = ta.scrollHeight + "px";
        });
    });

    // Save draft
    document.querySelectorAll(".draft-save-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            const draftId = btn.dataset.draftId;
            const textarea = document.querySelector(`.em-draft-textarea[data-draft-id="${draftId}"]`);
            const savedMsg = document.querySelector(`.em-saved-msg[data-saved-for="${draftId}"]`);

            btn.disabled = true;
            btn.textContent = "Saving...";

            try {
                const { error } = await supabase
                    .from("drafts")
                    .update({ draft_body: textarea.value, user_edited: true })
                    .eq("id", draftId);

                if (error) throw error;

                savedMsg.classList.add("visible");
                setTimeout(() => savedMsg.classList.remove("visible"), 2000);
            } catch (err) {
                showError(`Failed to save draft: ${err.message}`);
            } finally {
                btn.disabled = false;
                btn.textContent = "Save";
            }
        });
    });

    // Mark completed
    document.querySelectorAll(".mark-completed-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            const emailId = btn.dataset.emailId;
            btn.disabled = true;
            btn.textContent = "Updating...";

            try {
                const { error } = await supabase
                    .from("emails")
                    .update({ status: "completed" })
                    .eq("id", emailId);

                if (error) throw error;

                // Update local state and re-render
                const email = allEmails.find(e => e.id === emailId);
                if (email) email.status = "completed";
                renderEmails();
            } catch (err) {
                showError(`Failed to mark completed: ${err.message}`);
            }
        });
    });
}

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadEmails();
