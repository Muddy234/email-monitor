/**
 * Panel 4: Pipeline Trace
 * Select an email → vertical trace showing all 7 pipeline stages.
 *
 * Stages:
 *   1. Raw Email — sender, subject, time, body preview
 *   2. Filter — was it filtered out? why?
 *   3. Score — signals, raw/calibrated score, tier, gate decision
 *   4. Enrich — contact info, thread context
 *   5. Classify — needs_response, confidence, archetype, reason
 *   6. Draft — generated draft body
 *   7. Delivery — outlook draft status
 */
import { supabase } from "../supabase-client.js";
import { escapeHtml, formatDate } from "../ui.js";
import { createEmailPicker, clearEmailCache } from "./email-picker.js";

export async function initPipelineTrace() {
    clearEmailCache();
    const panel = document.getElementById("panel-trace");

    const { data: { user } } = await supabase.auth.getUser();
    if (!user) { panel.innerHTML = `<div class="em-empty">Not authenticated.</div>`; return; }

    panel.innerHTML = `
        <h3 class="em-section-title">Select Email</h3>
        <div id="dt-trace-picker" style="margin-bottom:24px"></div>
        <div id="dt-trace-detail"></div>
    `;

    createEmailPicker(
        document.getElementById("dt-trace-picker"),
        (email) => renderTrace(email, user.id)
    );
}

async function renderTrace(email, userId) {
    const detail = document.getElementById("dt-trace-detail");
    detail.innerHTML = `<div class="em-empty" style="padding:20px">Loading trace…</div>`;

    // Fetch all related data fresh from DB
    const [evtRes, contactRes, clsRes, draftRes] = await Promise.all([
        supabase.from("response_events").select("*").eq("user_id", userId).eq("email_id", email.id).limit(1),
        email.sender_email
            ? supabase.from("contacts").select("contact_type, organization, role, relationship_significance, total_received, avg_response_time_hours").eq("user_id", userId).eq("email", email.sender_email.toLowerCase()).single()
            : Promise.resolve({ data: null }),
        supabase.from("classifications").select("*").eq("email_id", email.id).limit(1),
        supabase.from("drafts").select("*").eq("email_id", email.id).limit(1),
    ]);

    const evt = evtRes.data?.[0];
    const contact = contactRes.data;
    const cls = clsRes.data?.[0] || email.classifications?.[0];
    const draft = draftRes.data?.[0] || email.drafts?.[0];

    // Determine filter status
    const wasFiltered = cls && !cls.needs_response && (
        cls.action === "skip" ||
        cls.context?.startsWith("Filtered") ||
        cls.context?.startsWith("Skipped:") ||
        cls.action?.startsWith("Auto-skipped")
    );
    const wasGated = evt?.gate_reason != null;

    // Body preview (first 500 chars)
    let bodyPreview = (email.body || "").substring(0, 500);
    if ((email.body || "").length > 500) bodyPreview += "\n[...]";

    detail.innerHTML = `
        <div class="em-card" style="padding:28px 24px">
            ${renderStage1(email, bodyPreview)}
            ${renderStage2(email, cls, wasFiltered)}
            ${renderStage3(evt, email, wasFiltered, wasGated)}
            ${renderStage4(contact, evt, email, wasFiltered, wasGated)}
            ${renderStage5(cls, email, wasFiltered, wasGated)}
            ${renderStage6(draft, cls, wasFiltered, wasGated)}
            ${renderStage7(draft)}
        </div>
    `;
}

// ─── Stage 1: Raw Email ──────────────────────────────────────
function renderStage1(email, bodyPreview) {
    return traceStage("Stage 1: Raw Email", "active", `
        <div class="em-kv-grid">
            <div class="em-kv-label">Sender</div>
            <div class="em-kv-value">${escapeHtml(email.sender_name || "")} &lt;${escapeHtml(email.sender_email || "")}&gt;</div>
            <div class="em-kv-label">Subject</div>
            <div class="em-kv-value">${escapeHtml(email.subject || "(no subject)")}</div>
            <div class="em-kv-label">Received</div>
            <div class="em-kv-value">${formatDate(email.received_time)}</div>
            <div class="em-kv-label">Status</div>
            <div class="em-kv-value"><span class="em-badge em-badge-slate">${escapeHtml(email.status || "—")}</span></div>
        </div>
        <div style="margin-top:12px;padding:10px 14px;background:var(--em-slate-50);border-radius:var(--em-radius-sm);font-size:12px;white-space:pre-wrap;max-height:200px;overflow-y:auto;color:var(--em-slate-600)">${escapeHtml(bodyPreview)}</div>
    `);
}

// ─── Stage 2: Filter ─────────────────────────────────────────
function renderStage2(email, cls, wasFiltered) {
    if (wasFiltered) {
        const reason = cls.context || cls.action || "Unknown filter rule";
        return traceStage("Stage 2: Filter", "stopped", `
            <div class="em-trace-verdict em-trace-verdict-stop">Filtered Out</div>
            <div class="em-kv-grid">
                <div class="em-kv-label">Result</div>
                <div class="em-kv-value"><span class="em-badge em-badge-slate">Skipped</span></div>
                <div class="em-kv-label">Reason</div>
                <div class="em-kv-value">${escapeHtml(reason)}</div>
            </div>
            <div class="em-trace-note">Pipeline stopped here — email did not proceed to scoring.</div>
        `);
    }

    return traceStage("Stage 2: Filter", "active", `
        <div class="em-trace-verdict em-trace-verdict-pass">Passed</div>
        <div class="em-kv-grid">
            <div class="em-kv-label">Result</div>
            <div class="em-kv-value"><span class="em-badge em-badge-green">Passed filters</span></div>
        </div>
    `);
}

// ─── Stage 3: Score ──────────────────────────────────────────
function renderStage3(evt, email, wasFiltered, wasGated) {
    if (wasFiltered) {
        return traceStage("Stage 3: Score", "skipped", `
            <div class="em-trace-note">Skipped — email was filtered in Stage 2.</div>
        `);
    }

    if (!evt) {
        if (email.status === "processing") {
            return traceStage("Stage 3: Score", "pending", `
                <div class="em-trace-verdict em-trace-verdict-pending">Processing</div>
                <div class="em-trace-note">Email is currently in the processing queue. Scoring data will appear once the worker completes this batch.</div>
            `);
        }
        if (email.status === "unprocessed") {
            return traceStage("Stage 3: Score", "pending", `
                <div class="em-trace-verdict em-trace-verdict-pending">Queued</div>
                <div class="em-trace-note">Email is queued but not yet claimed by the worker.</div>
            `);
        }
        return traceStage("Stage 3: Score", "empty", `
            <div class="em-trace-note">No scoring data found. Email may have been processed before scoring was added.</div>
        `);
    }

    const scoreDisplay = evt.calibrated_prob != null
        ? `${(evt.calibrated_prob * 100).toFixed(1)}%`
        : "—";
    const rawDisplay = evt.raw_score != null
        ? evt.raw_score.toFixed(3)
        : "—";

    let verdictHtml = "";
    if (wasGated) {
        verdictHtml = `<div class="em-trace-verdict em-trace-verdict-stop">Gated — ${escapeHtml(evt.gate_reason)}</div>`;
    } else {
        verdictHtml = `<div class="em-trace-verdict em-trace-verdict-pass">Passed scoring gate</div>`;
    }

    return traceStage("Stage 3: Score", wasGated ? "stopped" : "active", `
        ${verdictHtml}
        <div class="em-trace-score-bar">
            <div class="em-trace-score-fill" style="width:${Math.min(100, (evt.calibrated_prob || 0) * 100)}%"></div>
            <span class="em-trace-score-label">${scoreDisplay}</span>
        </div>
        <div class="em-kv-grid">
            <div class="em-kv-label">Raw Score</div>
            <div class="em-kv-value">${rawDisplay}</div>
            <div class="em-kv-label">Calibrated Probability</div>
            <div class="em-kv-value">${scoreDisplay}</div>
            <div class="em-kv-label">Confidence Tier</div>
            <div class="em-kv-value">${escapeHtml(evt.confidence_tier || "—")}</div>
            <div class="em-kv-label">User Position</div>
            <div class="em-kv-value">${escapeHtml(evt.user_position || "—")}</div>
            <div class="em-kv-label">Has Question</div>
            <div class="em-kv-value">${evt.has_question ? "Yes" : "No"}</div>
            <div class="em-kv-label">Has Action Language</div>
            <div class="em-kv-value">${evt.has_action_language ? "Yes" : "No"}</div>
            <div class="em-kv-label">Subject Type</div>
            <div class="em-kv-value">${escapeHtml(evt.subject_type || "—")}</div>
            <div class="em-kv-label">Total Recipients</div>
            <div class="em-kv-value">${evt.total_recipients ?? "—"}</div>
            <div class="em-kv-label">Is Recurring</div>
            <div class="em-kv-value">${evt.is_recurring ? "Yes" : "No"}</div>
        </div>
        ${wasGated ? '<div class="em-trace-note">Pipeline stopped here — email was gated by the scorer.</div>' : ""}
    `);
}

// ─── Stage 4: Enrich ─────────────────────────────────────────
function renderStage4(contact, evt, email, wasFiltered, wasGated) {
    if (wasFiltered || wasGated) {
        const reason = wasFiltered ? "filtered in Stage 2" : "gated in Stage 3";
        return traceStage("Stage 4: Enrich", "skipped", `
            <div class="em-trace-note">Skipped — email was ${reason}.</div>
        `);
    }

    if (!contact && !evt) {
        if (email.status === "processing" || email.status === "unprocessed") {
            return traceStage("Stage 4: Enrich", "pending", `
                <div class="em-trace-note">Waiting for earlier pipeline stages to complete.</div>
            `);
        }
        return traceStage("Stage 4: Enrich", "empty", `
            <div class="em-trace-note">No enrichment data available.</div>
        `);
    }

    return traceStage("Stage 4: Enrich", "active", `
        <div class="em-kv-grid">
            ${contact ? `
                <div class="em-kv-label">Contact Type</div>
                <div class="em-kv-value"><span class="em-badge em-badge-blue">${escapeHtml(contact.contact_type || "unknown")}</span></div>
                <div class="em-kv-label">Organization</div>
                <div class="em-kv-value">${escapeHtml(contact.organization || "—")}</div>
                <div class="em-kv-label">Role</div>
                <div class="em-kv-value">${escapeHtml(contact.role || "—")}</div>
                <div class="em-kv-label">Relationship</div>
                <div class="em-kv-value">${escapeHtml(contact.relationship_significance || "—")}</div>
                <div class="em-kv-label">Emails Received</div>
                <div class="em-kv-value">${contact.total_received ?? "—"}</div>
                <div class="em-kv-label">Avg Response Time</div>
                <div class="em-kv-value">${contact.avg_response_time_hours != null ? contact.avg_response_time_hours.toFixed(1) + "h" : "—"}</div>
            ` : `
                <div class="em-kv-label">Contact</div>
                <div class="em-kv-value" style="color:var(--em-slate-400)">No contact record found for sender</div>
            `}
            ${evt?.conversation_id ? `
                <div class="em-kv-label">Conversation ID</div>
                <div class="em-kv-value" style="font-family:monospace;font-size:11px">${escapeHtml(evt.conversation_id)}</div>
            ` : ""}
        </div>
    `);
}

// ─── Stage 5: Classify ───────────────────────────────────────
function renderStage5(cls, email, wasFiltered, wasGated) {
    if (wasFiltered || wasGated) {
        const reason = wasFiltered ? "filtered in Stage 2" : "gated in Stage 3";
        return traceStage("Stage 5: Classify", "skipped", `
            <div class="em-trace-note">Skipped — email was ${reason}.</div>
        `);
    }

    if (!cls) {
        return traceStage("Stage 5: Classify", "empty", `
            <div class="em-trace-note">Email not yet classified — may still be processing.</div>
        `);
    }

    // Skip filter-generated classifications (those are shown in Stage 2)
    if (cls.action === "skip" || cls.context?.startsWith("Filtered") || cls.context?.startsWith("Skipped:")) {
        return traceStage("Stage 5: Classify", "skipped", `
            <div class="em-trace-note">No AI classification — email was handled by filter rules.</div>
        `);
    }

    // Enriched fields (reason, archetype, confidence) are stored on the emails
    // table, not on classifications. Pull from the email row as primary source.
    const confidence = email.classification_confidence ?? cls.confidence;
    const archetype = email.archetype || cls.archetype || "";
    const reason = email.reason || cls.reason || cls.context || "";

    const needsBadge = cls.needs_response
        ? '<span class="em-badge em-badge-amber">Yes</span>'
        : '<span class="em-badge em-badge-slate">No</span>';

    return traceStage("Stage 5: Classify", "active", `
        <div class="em-trace-verdict ${cls.needs_response ? "em-trace-verdict-pass" : "em-trace-verdict-neutral"}">
            ${cls.needs_response ? "Needs Response" : "No Response Needed"}
        </div>
        <div class="em-kv-grid">
            <div class="em-kv-label">Needs Response</div>
            <div class="em-kv-value">${needsBadge}</div>
            <div class="em-kv-label">Confidence</div>
            <div class="em-kv-value">${confidence != null ? (confidence * 100).toFixed(0) + "%" : "—"}</div>
            <div class="em-kv-label">Archetype</div>
            <div class="em-kv-value">${escapeHtml(archetype || "—")}</div>
            <div class="em-kv-label">Reason</div>
            <div class="em-kv-value">${escapeHtml(reason || "—")}</div>
            <div class="em-kv-label">Action</div>
            <div class="em-kv-value">${escapeHtml(cls.action || "—")}</div>
            <div class="em-kv-label">Project</div>
            <div class="em-kv-value">${escapeHtml(cls.project || "—")}</div>
            <div class="em-kv-label">Priority</div>
            <div class="em-kv-value">${cls.priority === "x" || cls.priority === 1 ? '<span class="em-badge em-badge-red">Urgent</span>' : "Normal"}</div>
        </div>
    `);
}

// ─── Stage 6: Draft ──────────────────────────────────────────
function renderStage6(draft, cls, wasFiltered, wasGated) {
    if (wasFiltered || wasGated) {
        const reason = wasFiltered ? "filtered in Stage 2" : "gated in Stage 3";
        return traceStage("Stage 6: Draft", "skipped", `
            <div class="em-trace-note">Skipped — email was ${reason}.</div>
        `);
    }

    if (cls && !cls.needs_response) {
        return traceStage("Stage 6: Draft", "skipped", `
            <div class="em-trace-note">No draft needed — email was classified as not needing a response.</div>
        `);
    }

    if (!draft) {
        return traceStage("Stage 6: Draft", "empty", `
            <div class="em-trace-note">No draft generated yet. Email may still be processing, or it may be too old for drafting (&gt;24h).</div>
        `);
    }

    return traceStage("Stage 6: Draft", "active", `
        <div style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:var(--em-slate-700);padding:12px 16px;background:var(--em-slate-50);border-radius:var(--em-radius-sm);max-height:300px;overflow-y:auto">${escapeHtml(draft.draft_body)}</div>
        <div style="margin-top:8px;font-size:12px;color:var(--em-slate-400);display:flex;gap:16px">
            <span>${draft.user_edited ? "Edited by user" : "Auto-generated"}</span>
            <span>Status: ${escapeHtml(draft.status || "—")}</span>
            <span>Created: ${formatDate(draft.created_at)}</span>
        </div>
    `);
}

// ─── Stage 7: Delivery ───────────────────────────────────────
function renderStage7(draft) {
    if (!draft) {
        return traceStage("Stage 7: Delivery", "skipped", `
            <div class="em-trace-note">No draft to deliver.</div>
        `, true);
    }

    const hasOutlookId = !!draft.outlook_draft_id;
    const deliveredAt = draft.delivered_at;

    if (hasOutlookId || deliveredAt) {
        return traceStage("Stage 7: Delivery", "active", `
            <div class="em-trace-verdict em-trace-verdict-pass">Delivered to Outlook</div>
            <div class="em-kv-grid">
                <div class="em-kv-label">Outlook Draft ID</div>
                <div class="em-kv-value" style="font-family:monospace;font-size:11px">${escapeHtml(draft.outlook_draft_id || "—")}</div>
                ${deliveredAt ? `
                    <div class="em-kv-label">Delivered At</div>
                    <div class="em-kv-value">${formatDate(deliveredAt)}</div>
                ` : ""}
            </div>
        `, true);
    }

    return traceStage("Stage 7: Delivery", "pending", `
        <div class="em-trace-verdict em-trace-verdict-pending">Pending Delivery</div>
        <div class="em-trace-note">Draft is waiting for the browser extension to push it to Outlook.</div>
    `, true);
}

// ─── Shared stage renderer ───────────────────────────────────
// status: "active" | "stopped" | "skipped" | "empty" | "pending"
function traceStage(label, status, content, isLast = false) {
    const dotClass = {
        active: "",
        stopped: " em-trace-dot-stop",
        skipped: " em-trace-dot-empty",
        empty: " em-trace-dot-empty",
        pending: " em-trace-dot-pending",
    }[status] || " em-trace-dot-empty";

    return `
        <div class="em-trace-stage${isLast ? " last" : ""}">
            <div class="em-trace-dot${dotClass}"></div>
            <div class="em-trace-label">${label}</div>
            ${content}
        </div>
    `;
}
