/**
 * Emails page — three-tab layout: Drafts, Notable, All Emails.
 * Drafts: emails with AI-generated drafts ready in Outlook.
 * Notable: emails worth reading but no response needed.
 * All Emails: full list for last 30 days, searchable.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, showToast, getParam, setParam, formatDate, relativeTime, escapeHtml } from "../ui.js";
import { renderFeedbackControls, bindFeedbackEvents } from "../components/feedback.js";
import { traceStage, renderStage5_draft } from "../components/trace-renderers.js";

import { ensureAccess } from "../subscription.js";

await requireAuth();
await ensureAccess();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let allEmails = [];
let responseEvents = {};   // email_id → response_event row
let contacts = {};         // sender_email → contact row
let conversations = {};    // conversation_id → messages[]
let threadCounts = {};     // conversation_id → count of emails in thread
let searchQuery = getParam("q", "");
let activeTab = getParam("tab", "drafts");

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const container = document.getElementById("emailsContainer");
const searchInput = document.getElementById("searchInput");
const refreshBtn = document.getElementById("refreshBtn");
const tabs = document.querySelectorAll(".em-email-tab");

searchInput.value = searchQuery;

// -------------------------------------------------------------------------
// Tab switching
// -------------------------------------------------------------------------

tabs.forEach(tab => {
    tab.addEventListener("click", () => {
        tabs.forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        activeTab = tab.dataset.tab;
        setParam("tab", activeTab === "drafts" ? "" : activeTab);
        renderEmails();
    });
});

// Set initial active tab from URL
if (activeTab !== "drafts") {
    tabs.forEach(t => {
        t.classList.toggle("active", t.dataset.tab === activeTab);
    });
}

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
// Load data from Supabase
// -------------------------------------------------------------------------

async function loadEmails() {
    try {
        // Fetch emails with classifications and drafts
        const thirtyDaysAgo = new Date(Date.now() - 30 * 86400000).toISOString();
        const [emailsRes, eventsRes, contactsRes, convosRes] = await Promise.all([
            supabase
                .from("emails")
                .select("*, classifications(*), drafts(*)")
                .gte("received_time", thirtyDaysAgo)
                .order("received_time", { ascending: false }),
            supabase
                .from("response_events")
                .select("email_id, mc, ar, ub, dl, rt, target, pri, draft, reason, sender_tier, conversation_id, calibrated_prob, confidence_tier, gate_reason, summary"),
            supabase
                .from("contacts")
                .select("email, name, organization, contact_type, emails_per_month, is_vip, role, relationship_significance, total_received, avg_response_time_hours"),
            supabase
                .from("conversations")
                .select("conversation_id, messages"),
        ]);

        if (emailsRes.error) throw emailsRes.error;

        allEmails = emailsRes.data || [];

        // Index response events by email_id
        responseEvents = {};
        if (eventsRes.data) {
            for (const ev of eventsRes.data) {
                responseEvents[ev.email_id] = ev;
            }
        }

        // Index contacts by email
        contacts = {};
        if (contactsRes.data) {
            for (const c of contactsRes.data) {
                contacts[c.email.toLowerCase()] = c;
            }
        }

        // Index conversations by conversation_id
        conversations = {};
        if (convosRes.data) {
            for (const c of convosRes.data) {
                conversations[c.conversation_id] = c.messages || [];
            }
        }

        // Count emails per conversation thread
        threadCounts = {};
        for (const email of allEmails) {
            if (email.conversation_id) {
                threadCounts[email.conversation_id] = (threadCounts[email.conversation_id] || 0) + 1;
            }
        }

        renderEmails();
    } catch (err) {
        showError(`Failed to load emails: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Grouping logic
// -------------------------------------------------------------------------

function filterBySearch(emails) {
    if (!searchQuery) return emails;
    return emails.filter(e =>
        (e.sender || "").toLowerCase().includes(searchQuery) ||
        (e.sender_name || "").toLowerCase().includes(searchQuery) ||
        (e.subject || "").toLowerCase().includes(searchQuery)
    );
}

function hasDraft(email) {
    return email.drafts && email.drafts.length > 0;
}

function isNotable(email) {
    if (hasDraft(email)) return false;
    const cls = email.classifications?.[0];
    if (!cls) return false;
    if (cls.needs_response) return false; // needs response without draft = should be in drafts queue

    const ev = responseEvents[email.id];
    if (!ev) return false;

    // Notable if any of these signals are present
    return (
        ev.pri === "high" || ev.pri === "med" ||
        ev.mc === true ||
        ev.sender_tier === "C" || ev.sender_tier === "I" ||
        ev.rt !== "none"
    );
}

function groupEmails(emails) {
    const drafts = [];
    const notable = [];
    const other = [];

    for (const email of emails) {
        if (hasDraft(email)) {
            drafts.push(email);
        } else if (isNotable(email)) {
            notable.push(email);
        } else {
            other.push(email);
        }
    }

    return { drafts, notable, other };
}

// -------------------------------------------------------------------------
// Render
// -------------------------------------------------------------------------

function renderEmails() {
    const filtered = filterBySearch(allEmails);
    const groups = groupEmails(filtered);

    // Update tab counts
    document.getElementById("draftsCount").textContent = groups.drafts.length;
    document.getElementById("notableCount").textContent = groups.notable.length;
    document.getElementById("allCount").textContent = groups.other.length;

    let emails;
    let emptyMsg;

    if (activeTab === "drafts") {
        emails = groups.drafts;
        emptyMsg = "No drafts waiting — you're all caught up.";
    } else if (activeTab === "notable") {
        emails = groups.notable;
        emptyMsg = "Nothing notable right now.";
    } else {
        emails = groups.other;
        emptyMsg = allEmails.length === 0
            ? "No emails synced yet."
            : "No other emails to show.";
    }

    if (emails.length === 0) {
        showEmpty(container, emptyMsg);
        return;
    }

    let html = "";

    // Bulk action bar for drafts and notable tabs
    if (activeTab !== "all" && emails.length > 1) {
        html += `<div class="em-bulk-bar">
            <button class="em-btn em-btn-secondary em-btn-sm" id="markAllDoneBtn">Mark all done (${emails.length})</button>
        </div>`;
    }

    html += `<div class="em-email-list">`;
    for (const email of emails) {
        html += renderEmailCard(email);
    }
    html += `</div>`;

    container.innerHTML = html;
    bindCardEvents();
}

// -------------------------------------------------------------------------
// Pipeline trace (inline, compact)
// -------------------------------------------------------------------------

function renderEmailTrace(email) {
    const cls = email.classifications?.[0];
    const draft = email.drafts?.[0];
    const ev = responseEvents[email.id];
    const senderEmail = (email.sender_email || email.sender || "").toLowerCase();
    const contact = contacts[senderEmail];

    const wasFiltered = cls && !cls.needs_response && (
        cls.action === "skip" ||
        cls.context?.startsWith("Filtered") ||
        cls.context?.startsWith("Skipped:") ||
        cls.action?.startsWith("Auto-skipped")
    );

    // Stage 3 — signals only (compact)
    let stage3 = "";
    if (wasFiltered) {
        stage3 = traceStage("Signals", "skipped", `<div class="em-trace-note">Skipped — email was filtered.</div>`);
    } else if (ev && (ev.pri != null || ev.mc != null)) {
        const signalDefs = [
            { key: "mc", label: "Material" },
            { key: "ar", label: "Action Req" },
            { key: "ub", label: "Blocker" },
            { key: "dl", label: "Deadline" },
        ];
        const pills = signalDefs.map(s => {
            const on = ev[s.key] === true;
            const color = on ? "var(--em-amber-100)" : "var(--em-slate-50)";
            const textColor = on ? "var(--em-amber-700)" : "var(--em-slate-400)";
            return `<span style="display:inline-block;padding:3px 10px;margin:2px 4px 2px 0;background:${color};border-radius:var(--em-radius-sm);font-size:12px;color:${textColor};font-weight:${on ? 600 : 400}">${s.label}${on ? " ✓" : ""}</span>`;
        }).join("");
        stage3 = traceStage("Signals", "active", `<div style="display:flex;flex-wrap:wrap">${pills}</div>`);
    } else {
        stage3 = traceStage("Signals", "empty", `<div class="em-trace-note">No signal data.</div>`);
    }

    // Stage 4 — contact type, org, role only (compact)
    let stage4 = "";
    if (wasFiltered) {
        stage4 = traceStage("Context", "skipped", `<div class="em-trace-note">Skipped — email was filtered.</div>`);
    } else if (contact) {
        stage4 = traceStage("Context", "active", `
            <div class="em-kv-grid">
                <div class="em-kv-label">Contact Type</div>
                <div class="em-kv-value"><span class="em-badge em-badge-blue">${escapeHtml(contact.contact_type || "unknown")}</span></div>
                <div class="em-kv-label">Organization</div>
                <div class="em-kv-value">${escapeHtml(contact.organization || "—")}</div>
                <div class="em-kv-label">Role</div>
                <div class="em-kv-value">${escapeHtml(contact.role || "—")}</div>
            </div>
        `);
    } else {
        stage4 = traceStage("Context", "empty", `<div class="em-trace-note">No contact record found.</div>`);
    }

    return `
        ${stage3}
        ${stage4}
        ${renderStage5_draft(draft, ev, cls, wasFiltered)}
    `;
}

// -------------------------------------------------------------------------
// Email card rendering
// -------------------------------------------------------------------------

function renderEmailCard(email) {
    const cls = email.classifications?.[0];
    const draft = email.drafts?.[0];
    const ev = responseEvents[email.id];
    const isCompleted = email.status === "completed" || email.status === "dismissed";

    // Priority badge
    let priorityBadge = "";
    if (ev?.pri === "high") {
        priorityBadge = `<span class="em-badge em-badge-red">High</span>`;
    } else if (ev?.pri === "med") {
        priorityBadge = `<span class="em-badge em-badge-amber">Med</span>`;
    }

    // Contact context
    const senderEmail = (email.sender_email || email.sender || "").toLowerCase();
    const contact = contacts[senderEmail];
    let contactBadge = "";
    if (contact) {
        const parts = [];
        if (contact.contact_type && contact.contact_type !== "unknown") {
            const typeLabels = {
                internal: "Internal",
                external_legal: "Legal",
                external_lender: "Lender",
                external_vendor: "Vendor",
                investor: "Investor",
            };
            parts.push(typeLabels[contact.contact_type] || contact.contact_type);
        }
        if (contact.emails_per_month) {
            parts.push(`~${contact.emails_per_month}/mo`);
        }
        if (contact.is_vip) {
            parts.unshift("VIP");
        }
        if (parts.length > 0) {
            contactBadge = `<span class="em-contact-badge">${escapeHtml(parts.join(" · "))}</span>`;
        }
    }

    // Suggested action
    let actionHtml = "";
    if (cls && !isCompleted) {
        const reason = ev?.reason || cls.action || cls.context || "";
        if (reason) {
            actionHtml = `
                <div class="em-email-action-hint">
                    <span class="em-action-label">Suggested action:</span> ${escapeHtml(reason)}
                </div>
            `;
        }
    }

    // Draft preview (read-only)
    let draftHtml = "";
    if (draft) {
        draftHtml = `
            <div class="em-email-section-label">Draft Response</div>
            <div class="em-draft-preview">
                <div class="em-draft-preview-header">
                    <span>To: ${escapeHtml(email.sender_name || email.sender || "Unknown")}</span>
                </div>
                <div class="em-draft-preview-body">${escapeHtml(draft.draft_body || "")}</div>
            </div>
        `;
    }


    // Pipeline trace (collapsible, shown for all emails)
    const traceHtml = `
        <div class="em-trace-section">
            <button class="em-trace-toggle" data-email-id="${email.id}">
                <span class="em-email-section-label">Pipeline Trace</span>
                <span class="em-trace-toggle-icon">&#9660;</span>
            </button>
            <div class="em-trace-content" data-email-id="${email.id}" style="display: none;">
                ${renderEmailTrace(email)}
            </div>
        </div>
    `;

    // Thread view (conversation context)
    let threadHtml = "";
    if (email.conversation_id && conversations[email.conversation_id]?.length > 1) {
        const msgs = conversations[email.conversation_id];
        const threadItems = msgs.map(msg => {
            const isCurrentEmail = (msg.internet_message_id === email.message_id) || (msg.subject === email.subject && msg.from === email.sender);
            return `
                <div class="em-thread-message${isCurrentEmail ? " em-thread-current" : ""}">
                    <div class="em-thread-message-header">
                        <span class="em-thread-sender">${escapeHtml(msg.from || "Unknown")}</span>
                        <span class="em-thread-time">${msg.received ? relativeTime(msg.received) : ""}</span>
                    </div>
                    <div class="em-thread-snippet">${escapeHtml((msg.body || msg.snippet || "").substring(0, 150))}${(msg.body || msg.snippet || "").length > 150 ? "..." : ""}</div>
                </div>
            `;
        }).join("");

        threadHtml = `
            <div class="em-thread-section">
                <button class="em-thread-toggle" data-email-id="${email.id}">
                    <span class="em-email-section-label">Conversation (${msgs.length} messages)</span>
                    <span class="em-thread-toggle-icon">&#9660;</span>
                </button>
                <div class="em-thread-list" data-email-id="${email.id}" style="display: none;">
                    ${threadItems}
                </div>
            </div>
        `;
    }

    // Generate draft controls (for Notable emails or emails without drafts)
    let generateHtml = "";
    if (!draft && !isCompleted && activeTab !== "all") {
        generateHtml = `
            <div class="em-generate-section">
                <button class="em-btn em-btn-primary em-btn-sm generate-draft-btn" data-email-id="${email.id}">Draft a Reply</button>
                <button class="em-btn em-btn-secondary em-btn-sm generate-advanced-toggle" data-email-id="${email.id}">Options</button>
                <div class="em-generate-advanced" data-email-id="${email.id}" style="display: none;">
                    <textarea class="em-generate-instructions" data-email-id="${email.id}" placeholder="Any instructions? (e.g., 'Decline politely', 'Mention Tuesday call')" rows="2"></textarea>
                    <div class="em-tone-selector">
                        <span class="em-tone-label">Tone:</span>
                        <button class="em-tone-pill active" data-tone="professional" data-email-id="${email.id}">Professional</button>
                        <button class="em-tone-pill" data-tone="casual" data-email-id="${email.id}">Casual</button>
                        <button class="em-tone-pill" data-tone="brief" data-email-id="${email.id}">Brief</button>
                        <button class="em-tone-pill" data-tone="detailed" data-email-id="${email.id}">Detailed</button>
                    </div>
                </div>
            </div>
        `;
    }

    return `
        <div class="em-email-card${isCompleted ? " em-email-completed" : ""}" data-id="${email.id}">
            <div class="em-email-header">
                <button class="em-check-btn${isCompleted ? " em-check-done" : ""}" data-email-id="${email.id}" title="${isCompleted ? "Completed" : "Mark done"}">
                    ${isCompleted ? "&#10003;" : ""}
                </button>
                <div class="em-email-header-content">
                    <div>
                        <div class="em-email-sender">
                            ${escapeHtml(email.sender_name || email.sender || "Unknown")}
                            ${contactBadge}
                        </div>
                        <div class="em-email-subject">${escapeHtml(email.subject || "(no subject)")}</div>
                    </div>
                    <div class="em-email-header-badges">
                        <div class="em-email-meta">${relativeTime(email.received_time)} ${priorityBadge}</div>
                        ${draft ? `<span class="em-badge em-badge-blue">Draft</span>` : ""}
                        ${email.conversation_id && threadCounts[email.conversation_id] > 1 ? `<span class="em-badge em-badge-slate">${threadCounts[email.conversation_id]} msgs</span>` : ""}
                    </div>
                </div>
            </div>

            ${actionHtml}

            <div class="em-email-detail">
                ${ev?.summary ? `
                    <div class="em-email-section-label">Summary</div>
                    <div class="em-email-body em-email-summary">${escapeHtml(ev.summary)}</div>
                ` : `
                    <div class="em-email-section-label">Email Body</div>
                    <div class="em-email-body">${escapeHtml(email.body || "No body available.")}</div>
                `}

                ${draftHtml}
                ${traceHtml}
                ${threadHtml}
                ${generateHtml}

                ${!isCompleted ? `
                    <div class="em-detail-footer">
                        <button class="em-btn em-btn-secondary em-btn-sm mark-done-btn" data-email-id="${email.id}">Mark as done</button>
                        ${renderFeedbackControls(email.id, activeTab)}
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

    // Check button — toggle done/undone
    document.querySelectorAll(".em-check-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await toggleEmailDone(btn.dataset.emailId, btn);
        });
    });

    // Mark done (detail footer)
    document.querySelectorAll(".mark-done-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await markEmailDone(btn.dataset.emailId, btn);
        });
    });

    // Mark all done
    const markAllBtn = document.getElementById("markAllDoneBtn");
    if (markAllBtn) {
        markAllBtn.addEventListener("click", async () => {
            const groups = groupEmails(filterBySearch(allEmails));
            const emailsToMark = activeTab === "drafts" ? groups.drafts : groups.notable;
            if (emailsToMark.length === 0) return;

            markAllBtn.disabled = true;
            markAllBtn.textContent = "Marking...";

            try {
                const ids = emailsToMark.map(e => e.id);
                const newStatus = activeTab === "notable" ? "dismissed" : "completed";
                const { error } = await supabase
                    .from("emails")
                    .update({ status: newStatus })
                    .in("id", ids);

                if (error) throw error;

                for (const email of emailsToMark) {
                    email.status = newStatus;
                }
                renderEmails();
                showToast(`Marked ${ids.length} emails as done`);
            } catch (err) {
                markAllBtn.disabled = false;
                markAllBtn.textContent = `Mark all done (${emailsToMark.length})`;
                showError(`Failed to mark all: ${err.message}`);
            }
        });
    }

    // Generate draft
    document.querySelectorAll(".generate-draft-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
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
                btn.textContent = "Draft a Reply";
                showError(`Failed to queue draft: ${err.message}`);
            }
        });
    });

    // Advanced options toggle
    document.querySelectorAll(".generate-advanced-toggle").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            const panel = document.querySelector(`.em-generate-advanced[data-email-id="${emailId}"]`);
            if (panel) {
                const isHidden = panel.style.display === "none";
                panel.style.display = isHidden ? "block" : "none";
                btn.textContent = isHidden ? "Hide Options" : "Options";
            }
        });
    });

    // Tone pill selection
    document.querySelectorAll(".em-tone-pill").forEach(pill => {
        pill.addEventListener("click", (e) => {
            e.stopPropagation();
            const emailId = pill.dataset.emailId;
            document.querySelectorAll(`.em-tone-pill[data-email-id="${emailId}"]`).forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
        });
    });

    // Thread toggle
    document.querySelectorAll(".em-thread-toggle").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            const list = document.querySelector(`.em-thread-list[data-email-id="${emailId}"]`);
            const icon = btn.querySelector(".em-thread-toggle-icon");
            if (list) {
                const visible = list.style.display !== "none";
                list.style.display = visible ? "none" : "block";
                if (icon) icon.style.transform = visible ? "" : "rotate(180deg)";
            }
        });
    });

    // Trace toggle
    document.querySelectorAll(".em-trace-toggle").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const emailId = btn.dataset.emailId;
            const content = document.querySelector(`.em-trace-content[data-email-id="${emailId}"]`);
            const icon = btn.querySelector(".em-trace-toggle-icon");
            if (content) {
                const visible = content.style.display !== "none";
                content.style.display = visible ? "none" : "block";
                if (icon) icon.style.transform = visible ? "" : "rotate(180deg)";
            }
        });
    });

    // Feedback controls
    bindFeedbackEvents();
}

// -------------------------------------------------------------------------
// Actions
// -------------------------------------------------------------------------

async function markEmailDone(emailId, btn) {
    btn.disabled = true;
    if (btn.classList.contains("em-check-btn")) {
        btn.classList.add("em-check-saving");
    } else {
        btn.textContent = "Marking...";
    }

    try {
        const email = allEmails.find(e => e.id === emailId);
        const newStatus = (activeTab === "notable" && !hasDraft(email)) ? "dismissed" : "completed";

        const { error } = await supabase
            .from("emails")
            .update({ status: newStatus })
            .eq("id", emailId);

        if (error) throw error;

        if (email) email.status = newStatus;
        renderEmails();
        showToast("Marked as done");
    } catch (err) {
        btn.disabled = false;
        if (btn.classList.contains("em-check-btn")) {
            btn.classList.remove("em-check-saving");
        } else {
            btn.textContent = "Mark as done";
        }
        showError(`Failed to mark completed: ${err.message}`);
    }
}

async function toggleEmailDone(emailId, btn) {
    const email = allEmails.find(e => e.id === emailId);
    const isCompleted = email && (email.status === "completed" || email.status === "dismissed");

    if (isCompleted) {
        btn.disabled = true;
        btn.classList.add("em-check-saving");
        try {
            const { error } = await supabase
                .from("emails")
                .update({ status: "processed" })
                .eq("id", emailId);
            if (error) throw error;

            email.status = "processed";
            renderEmails();
            showToast("Marked as incomplete");
        } catch (err) {
            btn.disabled = false;
            btn.classList.remove("em-check-saving");
            showError(`Failed to update: ${err.message}`);
        }
    } else {
        await markEmailDone(emailId, btn);
    }
}

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadEmails();
