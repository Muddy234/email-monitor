/**
 * Panel 4: Pipeline Trace
 * Select an email → vertical trace showing all 6 pipeline stages.
 *
 * Stages:
 *   1. Raw Email — sender, subject, time, body preview
 *   2. Filter — was it filtered out? why?
 *   3. Signals — Haiku extraction: mc/ar/ub/dl/rt + pri/draft/reason
 *   4. Context — contact info, sender tier
 *   5. Draft — generated draft body
 *   6. Delivery — outlook draft status
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

    // Body preview (first 500 chars)
    let bodyPreview = (email.body || "").substring(0, 500);
    if ((email.body || "").length > 500) bodyPreview += "\n[...]";

    detail.innerHTML = `
        <div class="em-card" style="padding:28px 24px">
            ${renderStage1(email, bodyPreview)}
            ${renderStage2(email, cls, wasFiltered)}
            ${renderStage3_signals(evt, email, wasFiltered)}
            ${renderStage4_context(contact, evt, email, wasFiltered)}
            ${renderStage5_draft(draft, evt, cls, wasFiltered)}
            ${renderStage6_delivery(draft)}
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
            <div class="em-trace-note">Pipeline stopped here — email did not proceed to signal extraction.</div>
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

// ─── Stage 3: Signal Extraction ─────────────────────────────

/** Tier label map. */
const _tierLabels = { C: "Critical", I: "Internal", P: "Professional", U: "Unknown" };
const _tierBadge = { C: "red", I: "blue", P: "green", U: "slate" };

/** Response type label map. */
const _rtLabels = { none: "None", ack: "Acknowledge", ans: "Answer", act: "Action", dec: "Decision" };

function renderStage3_signals(evt, email, wasFiltered) {
    if (wasFiltered) {
        return traceStage("Stage 3: Signals", "skipped", `
            <div class="em-trace-note">Skipped — email was filtered in Stage 2.</div>
        `);
    }

    if (!evt) {
        if (email.status === "processing") {
            return traceStage("Stage 3: Signals", "pending", `
                <div class="em-trace-verdict em-trace-verdict-pending">Processing</div>
                <div class="em-trace-note">Email is in the processing queue. Signal data will appear once the worker completes this batch.</div>
            `);
        }
        if (email.status === "unprocessed") {
            return traceStage("Stage 3: Signals", "pending", `
                <div class="em-trace-verdict em-trace-verdict-pending">Queued</div>
                <div class="em-trace-note">Email is queued but not yet claimed by the worker.</div>
            `);
        }
        return traceStage("Stage 3: Signals", "empty", `
            <div class="em-trace-note">No signal data found. Email may have been processed before signal extraction was added.</div>
        `);
    }

    // Check if this is a new-pipeline event (has signal fields) vs legacy
    const hasSignals = evt.pri != null || evt.mc != null;

    if (!hasSignals) {
        // Legacy scoring data — show abbreviated view
        const scoreDisplay = evt.calibrated_prob != null
            ? `${(evt.calibrated_prob * 100).toFixed(1)}%`
            : "—";
        return traceStage("Stage 3: Signals", "active", `
            <div class="em-trace-note" style="margin-bottom:8px">Processed with legacy scoring pipeline.</div>
            <div class="em-kv-grid">
                <div class="em-kv-label">Calibrated Score</div>
                <div class="em-kv-value">${scoreDisplay}</div>
                <div class="em-kv-label">Confidence Tier</div>
                <div class="em-kv-value"><span class="em-badge em-badge-${evt.confidence_tier === "strong" ? "green" : evt.confidence_tier === "likely" ? "blue" : "slate"}">${escapeHtml(evt.confidence_tier || "—")}</span></div>
                ${evt.gate_reason ? `
                    <div class="em-kv-label">Gate</div>
                    <div class="em-kv-value"><span class="em-badge em-badge-red">Gated: ${escapeHtml(evt.gate_reason)}</span></div>
                ` : ""}
            </div>
        `);
    }

    // New signal extraction data
    const priColor = evt.pri === "high" ? "red" : evt.pri === "med" ? "amber" : "slate";
    const tierKey = evt.sender_tier || "U";

    // Build signal pills
    const signalDefs = [
        { key: "mc", label: "Material", desc: "Financial/legal consequence" },
        { key: "ar", label: "Action Req", desc: "Sender needs recipient action" },
        { key: "ub", label: "Blocker", desc: "Someone blocked on recipient" },
        { key: "dl", label: "Deadline", desc: "Time constraint" },
    ];
    const signalPills = signalDefs.map(s => {
        const on = evt[s.key] === true;
        const color = on ? "var(--em-amber-100)" : "var(--em-slate-50)";
        const textColor = on ? "var(--em-amber-700)" : "var(--em-slate-400)";
        return `<span title="${s.desc}" style="display:inline-block;padding:3px 10px;margin:2px 4px 2px 0;background:${color};border-radius:var(--em-radius-sm);font-size:12px;color:${textColor};font-weight:${on ? 600 : 400}">${s.label}${on ? " ✓" : ""}</span>`;
    }).join("");

    const draftBadge = evt.draft
        ? '<span class="em-badge em-badge-green">Yes</span>'
        : '<span class="em-badge em-badge-slate">No</span>';

    return traceStage("Stage 3: Signals", "active", `
        <div class="em-trace-verdict ${evt.draft ? "em-trace-verdict-pass" : "em-trace-verdict-neutral"}">
            ${evt.draft ? "Draft Recommended" : "No Draft Needed"}
        </div>
        <div class="em-kv-grid">
            <div class="em-kv-label">Priority</div>
            <div class="em-kv-value"><span class="em-badge em-badge-${priColor}">${escapeHtml((evt.pri || "low").toUpperCase())}</span></div>
            <div class="em-kv-label">Sender Tier</div>
            <div class="em-kv-value"><span class="em-badge em-badge-${_tierBadge[tierKey] || "slate"}">${escapeHtml(tierKey)} — ${escapeHtml(_tierLabels[tierKey] || "Unknown")}</span></div>
            <div class="em-kv-label">Response Type</div>
            <div class="em-kv-value">${escapeHtml(_rtLabels[evt.rt] || evt.rt || "—")}</div>
            <div class="em-kv-label">Draft</div>
            <div class="em-kv-value">${draftBadge}</div>
        </div>
        <div style="margin-top:14px">
            <div style="font-size:12px;font-weight:600;color:var(--em-slate-500);margin-bottom:6px">Signals</div>
            <div style="display:flex;flex-wrap:wrap">${signalPills}</div>
        </div>
        ${evt.reason ? `
            <div style="margin-top:14px">
                <div style="font-size:12px;font-weight:600;color:var(--em-slate-500);margin-bottom:4px">Reason</div>
                <div style="font-size:13px;color:var(--em-slate-600);line-height:1.5">${escapeHtml(evt.reason)}</div>
            </div>
        ` : ""}
    `);
}

// ─── Stage 4: Context ────────────────────────────────────────
function renderStage4_context(contact, evt, email, wasFiltered) {
    if (wasFiltered) {
        return traceStage("Stage 4: Context", "skipped", `
            <div class="em-trace-note">Skipped — email was filtered in Stage 2.</div>
        `);
    }

    if (!contact && !evt) {
        if (email.status === "processing" || email.status === "unprocessed") {
            return traceStage("Stage 4: Context", "pending", `
                <div class="em-trace-note">Waiting for earlier pipeline stages to complete.</div>
            `);
        }
        return traceStage("Stage 4: Context", "empty", `
            <div class="em-trace-note">No context data available.</div>
        `);
    }

    return traceStage("Stage 4: Context", "active", `
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

// ─── Stage 5: Draft ──────────────────────────────────────────
function renderStage5_draft(draft, evt, cls, wasFiltered) {
    if (wasFiltered) {
        return traceStage("Stage 5: Draft", "skipped", `
            <div class="em-trace-note">Skipped — email was filtered in Stage 2.</div>
        `);
    }

    // Check draft decision — prefer new signal field, fall back to old classification
    const draftNeeded = evt?.draft ?? cls?.needs_response;
    if (draftNeeded === false) {
        return traceStage("Stage 5: Draft", "skipped", `
            <div class="em-trace-note">No draft needed — signal extraction determined no response is warranted.</div>
        `);
    }

    if (!draft) {
        return traceStage("Stage 5: Draft", "empty", `
            <div class="em-trace-note">No draft generated yet. Email may still be processing, or it may be too old for drafting (&gt;24h).</div>
        `);
    }

    return traceStage("Stage 5: Draft", "active", `
        <div style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:var(--em-slate-700);padding:12px 16px;background:var(--em-slate-50);border-radius:var(--em-radius-sm);max-height:300px;overflow-y:auto">${escapeHtml(draft.draft_body)}</div>
        <div style="margin-top:8px;font-size:12px;color:var(--em-slate-400);display:flex;gap:16px">
            <span>${draft.user_edited ? "Edited by user" : "Auto-generated"}</span>
            <span>Status: ${escapeHtml(draft.status || "—")}</span>
            <span>Created: ${formatDate(draft.created_at)}</span>
        </div>
    `);
}

// ─── Stage 6: Delivery ───────────────────────────────────────
function renderStage6_delivery(draft) {
    if (!draft) {
        return traceStage("Stage 6: Delivery", "skipped", `
            <div class="em-trace-note">No draft to deliver.</div>
        `, true);
    }

    const hasOutlookId = !!draft.outlook_draft_id;
    const deliveredAt = draft.delivered_at;

    if (hasOutlookId || deliveredAt) {
        return traceStage("Stage 6: Delivery", "active", `
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

    return traceStage("Stage 6: Delivery", "pending", `
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
