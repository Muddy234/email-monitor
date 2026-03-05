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
let activeSection = getParam("section", ""); // from dashboard links

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

// Map section param to group name
const SECTION_MAP = {
    "drafts": "Drafts Ready",
    "needs-response": "Needs Response",
    "other": "Other",
    "completed": "Completed",
};

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

    // Determine group order — if section param, put that group first
    const groupOrder = ["Drafts Ready", "Needs Response", "Other", "Completed"];
    const targetGroup = SECTION_MAP[activeSection];

    for (const groupName of groupOrder) {
        const emails = groups[groupName];
        if (!emails || emails.length === 0) continue;

        const sectionId = Object.entries(SECTION_MAP).find(([, v]) => v === groupName)?.[0] || "";
        const isTarget = groupName === targetGroup;

        html += `<div class="em-group" id="section-${sectionId}">`;
        html += `<div class="em-group-title">${groupName}<span class="em-group-count">${emails.length}</span></div>`;
        html += `<div class="em-email-list">`;

        for (const email of emails) {
            html += renderEmailCard(email, groupName);
        }

        html += `</div></div>`;
    }

    container.innerHTML = html;
    bindCardEvents();

    // Scroll to target section if linked from dashboard
    if (targetGroup) {
        const sectionId = activeSection;
        const el = document.getElementById(`section-${sectionId}`);
        if (el) {
            el.scrollIntoView({ behavior: "smooth", block: "start" });
            activeSection = ""; // only scroll once
            setParam("section", "");
        }
    }
}

function renderEmailCard(email, groupName) {
    const cls = email.classifications?.[0];
    const draft = email.drafts?.[0];
    const priorityBadge = cls?.priority >= 3
        ? `<span class="em-badge em-badge-red">P${cls.priority}</span>`
        : cls?.priority >= 2
        ? `<span class="em-badge em-badge-amber">P${cls.priority}</span>`
        : "";

    const isCompleted = email.status === "completed";
    const needsResponse = groupName === "Needs Response";

    // Build summary for Needs Response emails
    let summaryHtml = "";
    if (needsResponse && cls) {
        const context = cls.context || "";
        const action = cls.action || "";
        summaryHtml = `
            <div class="em-email-summary">
                <div class="em-email-summary-label">Summary</div>
                <div class="em-email-summary-text">${escapeHtml(context || "No summary available.")}</div>
                ${action ? `
                    <div class="em-email-response-needed">
                        <span class="em-response-needed-label">Response Needed:</span>
                        ${escapeHtml(action)}
                    </div>
                ` : ""}
            </div>
        `;
    }

    return `
        <div class="em-email-card${isCompleted ? " em-email-completed" : ""}" data-id="${email.id}">
            <div class="em-email-header">
                <div class="em-email-checkbox-wrap">
                    <input type="checkbox" class="em-email-checkbox mark-completed-cb"
                        data-email-id="${email.id}"
                        ${isCompleted ? "checked disabled" : ""}
                        title="${isCompleted ? "Completed" : "Mark as completed"}">
                </div>
                <div class="em-email-header-content">
                    <div>
                        <div class="em-email-sender">${escapeHtml(email.sender_name || email.sender || "Unknown")}</div>
                        <div class="em-email-subject">${escapeHtml(email.subject || "(no subject)")}</div>
                    </div>
                    <div style="text-align: right; flex-shrink: 0;">
                        <div class="em-email-meta">${formatDate(email.received_time)} ${priorityBadge}</div>
                        ${draft ? `<span class="em-badge em-badge-blue" style="margin-top: 4px;">Draft</span>` : ""}
                        ${isCompleted ? `<span class="em-badge em-badge-green" style="margin-top: 4px;">Done</span>` : ""}
                    </div>
                </div>
            </div>

            ${summaryHtml}

            <div class="em-email-detail">
                <div class="em-email-section-label">Email Body</div>
                <div class="em-email-body">${escapeHtml(email.body || "No body available.")}</div>

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

                ${!draft && !isCompleted ? `
                    <div style="margin-top: 12px;">
                        <button class="em-btn em-btn-primary em-btn-sm generate-draft-btn" data-email-id="${email.id}">Generate Draft</button>
                    </div>
                ` : ""}
            </div>
        </div>
    `;
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
            if (e.target.closest("button") || e.target.closest("textarea") || e.target.closest("input")) return;
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

    // Mark completed via checkbox
    document.querySelectorAll(".mark-completed-cb").forEach(cb => {
        cb.addEventListener("change", async (e) => {
            if (!cb.checked) return; // only handle checking, not unchecking
            const emailId = cb.dataset.emailId;
            cb.disabled = true;

            try {
                const { error } = await supabase
                    .from("emails")
                    .update({ status: "completed" })
                    .eq("id", emailId);

                if (error) throw error;

                const email = allEmails.find(e => e.id === emailId);
                if (email) email.status = "completed";
                renderEmails();
            } catch (err) {
                cb.checked = false;
                cb.disabled = false;
                showError(`Failed to mark completed: ${err.message}`);
            }
        });
    });

    // Generate draft
    document.querySelectorAll(".generate-draft-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            const emailId = btn.dataset.emailId;
            btn.disabled = true;
            btn.textContent = "Generating...";

            try {
                // Mark email as unprocessed so the worker picks it up for draft generation
                const { error } = await supabase
                    .from("emails")
                    .update({ status: "unprocessed" })
                    .eq("id", emailId);

                if (error) throw error;

                btn.textContent = "Queued";
                btn.classList.remove("em-btn-primary");
                btn.classList.add("em-btn-secondary");

                // Update local state
                const email = allEmails.find(e => e.id === emailId);
                if (email) email.status = "unprocessed";
            } catch (err) {
                btn.disabled = false;
                btn.textContent = "Generate Draft";
                showError(`Failed to queue draft: ${err.message}`);
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
