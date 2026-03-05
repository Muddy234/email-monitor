/**
 * Emails page — grouped list with inline detail, draft preview/editing, mark done.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, showToast, getParam, setParam, formatDate } from "../ui.js";

await requireAuth();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let allEmails = [];
let searchQuery = getParam("q", "");
let activeSection = getParam("section", ""); // from dashboard links

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const container = document.getElementById("emailsContainer");
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

refreshBtn.addEventListener("click", () => loadEmails());

// -------------------------------------------------------------------------
// Load emails from Supabase
// -------------------------------------------------------------------------

async function loadEmails() {
    try {
        const { data, error } = await supabase
            .from("emails")
            .select("*, classifications(*), drafts(*)")
            .order("received_time", { ascending: false });

        if (error) throw error;

        allEmails = data || [];
        renderEmails();
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

// How many cards to show per group before "Show more"
const GROUP_LIMITS = {
    "Drafts Ready": Infinity,
    "Needs Response": Infinity,
    "Other": 20,
    "Completed": 10,
};

// Track which groups have been expanded by the user
const expandedGroups = new Set();

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

    const groupOrder = ["Drafts Ready", "Needs Response", "Other", "Completed"];
    const targetGroup = SECTION_MAP[activeSection];

    for (const groupName of groupOrder) {
        const emails = groups[groupName];
        if (!emails || emails.length === 0) continue;

        const sectionId = Object.entries(SECTION_MAP).find(([, v]) => v === groupName)?.[0] || "";
        const limit = GROUP_LIMITS[groupName] || Infinity;
        const isExpanded = expandedGroups.has(groupName);
        const visibleEmails = isExpanded ? emails : emails.slice(0, limit);
        const hasMore = emails.length > limit && !isExpanded;

        html += `<div class="em-group em-fade-in" id="section-${sectionId}">`;
        html += `<div class="em-group-title">${groupName}<span class="em-group-count">${emails.length}</span></div>`;
        html += `<div class="em-email-list">`;

        for (const email of visibleEmails) {
            html += renderEmailCard(email);
        }

        html += `</div>`;

        if (hasMore) {
            html += `<button class="em-btn em-btn-secondary em-btn-sm em-show-more-btn" data-group="${groupName}" style="margin: 12px auto; display: block;">Show all ${emails.length} ${groupName.toLowerCase()} emails</button>`;
        }

        html += `</div>`;
    }

    container.innerHTML = html;
    bindCardEvents();
    bindShowMoreButtons();

    // Scroll to target section if linked from dashboard
    if (targetGroup) {
        const el = document.getElementById(`section-${activeSection}`);
        if (el) {
            el.scrollIntoView({ behavior: "smooth", block: "start" });
            activeSection = "";
            setParam("section", "");
        }
    }
}

function bindShowMoreButtons() {
    document.querySelectorAll(".em-show-more-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            expandedGroups.add(btn.dataset.group);
            renderEmails();
        });
    });
}

function renderEmailCard(email) {
    const cls = email.classifications?.[0];
    const draft = email.drafts?.[0];
    const priorityBadge = cls?.priority >= 3
        ? `<span class="em-badge em-badge-red">P${cls.priority}</span>`
        : cls?.priority >= 2
        ? `<span class="em-badge em-badge-amber">P${cls.priority}</span>`
        : "";

    const isCompleted = email.status === "completed";

    // Build summary for any email with classification context
    let summaryHtml = "";
    if (cls && !isCompleted) {
        const context = cls.context || "";
        const action = cls.action || "";
        if (context || action) {
            summaryHtml = `
                <div class="em-email-summary">
                    <div class="em-email-summary-text">${escapeHtml(context || "No summary available.")}</div>
                    ${action ? `
                        <div class="em-email-response-needed">
                            <span class="em-response-needed-label">Response needed:</span>
                            ${escapeHtml(action)}
                        </div>
                    ` : ""}
                </div>
            `;
        }
    }

    // Draft section — preview mode by default, edit mode on click
    let draftHtml = "";
    if (draft) {
        draftHtml = `
            <div class="em-email-section-label">Draft Response</div>
            <div class="em-draft-preview" data-draft-id="${draft.id}">
                <div class="em-draft-preview-header">
                    <span>To: ${escapeHtml(email.sender || "Unknown")}</span>
                </div>
                <div class="em-draft-preview-body">${escapeHtml(draft.draft_body || "")}</div>
            </div>
            <div class="em-draft-editor" data-draft-id="${draft.id}" style="display: none;">
                <textarea class="em-draft-textarea" data-draft-id="${draft.id}">${escapeHtml(draft.draft_body || "")}</textarea>
            </div>
            <div class="em-draft-actions">
                <button class="em-btn em-btn-secondary em-btn-sm draft-edit-btn" data-draft-id="${draft.id}">Edit</button>
                <button class="em-btn em-btn-primary em-btn-sm draft-save-btn" data-draft-id="${draft.id}" style="display: none;">Save</button>
                <button class="em-btn em-btn-secondary em-btn-sm draft-cancel-btn" data-draft-id="${draft.id}" style="display: none;">Cancel</button>
                <button class="em-btn em-btn-danger em-btn-sm draft-delete-btn" data-draft-id="${draft.id}">Delete</button>
            </div>
        `;
    }

    return `
        <div class="em-email-card${isCompleted ? " em-email-completed" : ""}" data-id="${email.id}">
            <div class="em-email-header">
                <div class="em-email-header-content">
                    <div>
                        <div class="em-email-sender">${escapeHtml(email.sender_name || email.sender || "Unknown")}</div>
                        <div class="em-email-subject">${escapeHtml(email.subject || "(no subject)")}</div>
                    </div>
                    <div class="em-email-header-badges">
                        <div class="em-email-meta">${formatDate(email.received_time)} ${priorityBadge}</div>
                        ${draft ? `<span class="em-badge em-badge-blue">Draft</span>` : ""}
                        ${isCompleted ? `<span class="em-badge em-badge-green">Done</span>` : ""}
                    </div>
                </div>
            </div>

            ${summaryHtml}

            <div class="em-email-detail">
                <div class="em-email-section-label">Email Body</div>
                <div class="em-email-body">${escapeHtml(email.body || "No body available.")}</div>

                ${draftHtml}

                ${!draft && !isCompleted ? `
                    <div style="margin-top: 12px;">
                        <button class="em-btn em-btn-primary em-btn-sm generate-draft-btn" data-email-id="${email.id}">Generate Draft</button>
                    </div>
                ` : ""}

                ${!isCompleted ? `
                    <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--em-slate-200);">
                        <button class="em-btn em-btn-secondary em-btn-sm mark-done-btn" data-email-id="${email.id}">Mark done</button>
                    </div>
                ` : ""}
            </div>
        </div>
    `;
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

    // Edit draft — switch to textarea mode
    document.querySelectorAll(".draft-edit-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const draftId = btn.dataset.draftId;
            const preview = document.querySelector(`.em-draft-preview[data-draft-id="${draftId}"]`);
            const editor = document.querySelector(`.em-draft-editor[data-draft-id="${draftId}"]`);
            const saveBtn = document.querySelector(`.draft-save-btn[data-draft-id="${draftId}"]`);
            const cancelBtn = document.querySelector(`.draft-cancel-btn[data-draft-id="${draftId}"]`);

            if (preview) preview.style.display = "none";
            if (editor) editor.style.display = "block";
            btn.style.display = "none";
            if (saveBtn) saveBtn.style.display = "";
            if (cancelBtn) cancelBtn.style.display = "";

            // Auto-resize textarea
            const ta = editor?.querySelector("textarea");
            if (ta) {
                ta.style.height = "auto";
                ta.style.height = ta.scrollHeight + "px";
                ta.focus();
            }
        });
    });

    // Cancel draft edit — switch back to preview
    document.querySelectorAll(".draft-cancel-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const draftId = btn.dataset.draftId;
            const preview = document.querySelector(`.em-draft-preview[data-draft-id="${draftId}"]`);
            const editor = document.querySelector(`.em-draft-editor[data-draft-id="${draftId}"]`);
            const editBtn = document.querySelector(`.draft-edit-btn[data-draft-id="${draftId}"]`);
            const saveBtn = document.querySelector(`.draft-save-btn[data-draft-id="${draftId}"]`);

            if (preview) preview.style.display = "";
            if (editor) editor.style.display = "none";
            if (editBtn) editBtn.style.display = "";
            if (saveBtn) saveBtn.style.display = "none";
            btn.style.display = "none";
        });
    });

    // Save draft
    document.querySelectorAll(".draft-save-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            const draftId = btn.dataset.draftId;
            const textarea = document.querySelector(`.em-draft-textarea[data-draft-id="${draftId}"]`);

            btn.disabled = true;
            btn.textContent = "Saving...";

            try {
                const { error } = await supabase
                    .from("drafts")
                    .update({ draft_body: textarea.value, user_edited: true })
                    .eq("id", draftId);

                if (error) throw error;

                // Update preview body
                const previewBody = document.querySelector(`.em-draft-preview[data-draft-id="${draftId}"] .em-draft-preview-body`);
                if (previewBody) previewBody.textContent = textarea.value;

                // Switch back to preview mode
                const preview = document.querySelector(`.em-draft-preview[data-draft-id="${draftId}"]`);
                const editor = document.querySelector(`.em-draft-editor[data-draft-id="${draftId}"]`);
                const editBtn = document.querySelector(`.draft-edit-btn[data-draft-id="${draftId}"]`);
                const cancelBtn = document.querySelector(`.draft-cancel-btn[data-draft-id="${draftId}"]`);

                if (preview) preview.style.display = "";
                if (editor) editor.style.display = "none";
                if (editBtn) editBtn.style.display = "";
                if (cancelBtn) cancelBtn.style.display = "none";

                showToast("Draft saved");
            } catch (err) {
                showError(`Failed to save draft: ${err.message}`);
            } finally {
                btn.disabled = false;
                btn.textContent = "Save";
                btn.style.display = "none";
            }
        });
    });

    // Delete draft
    document.querySelectorAll(".draft-delete-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            const draftId = btn.dataset.draftId;
            btn.disabled = true;
            btn.textContent = "Deleting...";

            try {
                const { error } = await supabase
                    .from("drafts")
                    .delete()
                    .eq("id", draftId);

                if (error) throw error;

                // Remove draft from local state and re-render
                for (const email of allEmails) {
                    if (email.drafts) {
                        email.drafts = email.drafts.filter(d => d.id !== draftId);
                    }
                }
                renderEmails();
                showToast("Draft deleted");
            } catch (err) {
                btn.disabled = false;
                btn.textContent = "Delete";
                showError(`Failed to delete draft: ${err.message}`);
            }
        });
    });

    // Mark done
    document.querySelectorAll(".mark-done-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            const emailId = btn.dataset.emailId;
            btn.disabled = true;
            btn.textContent = "Marking...";

            try {
                const { error } = await supabase
                    .from("emails")
                    .update({ status: "completed" })
                    .eq("id", emailId);

                if (error) throw error;

                const email = allEmails.find(e => e.id === emailId);
                if (email) email.status = "completed";
                renderEmails();
                showToast("Marked as done");
            } catch (err) {
                btn.disabled = false;
                btn.textContent = "Mark done";
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
                const { error } = await supabase
                    .from("emails")
                    .update({ status: "unprocessed" })
                    .eq("id", emailId);

                if (error) throw error;

                btn.textContent = "Queued";
                btn.classList.remove("em-btn-primary");
                btn.classList.add("em-btn-secondary");

                const email = allEmails.find(e => e.id === emailId);
                if (email) email.status = "unprocessed";

                showToast("Draft generation queued");
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
