/**
 * Panel 4: Pipeline Trace
 * Select an email → vertical trace showing all pipeline stages.
 */
import { supabase } from "../supabase-client.js";
import { escapeHtml, formatDate } from "../ui.js";
import { createEmailPicker } from "./email-picker.js";

export async function initPipelineTrace() {
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

    // Fetch response_event + contact in parallel
    const [evtRes, contactRes] = await Promise.all([
        supabase.from("response_events").select("*").eq("user_id", userId).eq("email_id", email.id).limit(1),
        email.sender_email
            ? supabase.from("contacts").select("contact_type, organization, role, relationship_significance").eq("user_id", userId).eq("email", email.sender_email.toLowerCase()).single()
            : Promise.resolve({ data: null }),
    ]);

    const evt = evtRes.data?.[0];
    const contact = contactRes.data;
    const cls = email.classifications?.[0];
    const draft = email.drafts?.[0];

    // Body preview (first 500 chars)
    let bodyPreview = (email.body || "").substring(0, 500);
    if ((email.body || "").length > 500) bodyPreview += "\n[...]";

    detail.innerHTML = `
        <div class="em-card" style="padding:28px 24px">
            ${traceStage("Stage 1: Raw Email", true, `
                <div class="em-kv-grid">
                    <div class="em-kv-label">Sender</div>
                    <div class="em-kv-value">${escapeHtml(email.sender_name || "")} &lt;${escapeHtml(email.sender_email || "")}&gt;</div>
                    <div class="em-kv-label">Subject</div>
                    <div class="em-kv-value">${escapeHtml(email.subject || "(no subject)")}</div>
                    <div class="em-kv-label">Received</div>
                    <div class="em-kv-value">${formatDate(email.received_time)}</div>
                    <div class="em-kv-label">Status</div>
                    <div class="em-kv-value"><span class="em-badge em-badge-slate">${escapeHtml(email.status || "—")}</span></div>
                    ${contact ? `
                        <div class="em-kv-label">Contact Type</div>
                        <div class="em-kv-value"><span class="em-badge em-badge-blue">${escapeHtml(contact.contact_type || "unknown")}</span> ${escapeHtml(contact.organization || "")}</div>
                    ` : ""}
                </div>
                <div style="margin-top:12px;padding:10px 14px;background:var(--em-slate-50);border-radius:var(--em-radius-sm);font-size:12px;white-space:pre-wrap;max-height:200px;overflow-y:auto;color:var(--em-slate-600)">${escapeHtml(bodyPreview)}</div>
            `)}

            ${traceStage("Stage 2: Scoring Features", !!evt, evt ? `
                <div class="em-kv-grid">
                    <div class="em-kv-label">Responded</div>
                    <div class="em-kv-value">${evt.responded ? "Yes" : "No"}</div>
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
                    <div class="em-kv-label">Response Latency</div>
                    <div class="em-kv-value">${evt.response_latency_hours != null ? evt.response_latency_hours.toFixed(1) + "h" : "—"}</div>
                </div>
            ` : `<div style="color:var(--em-slate-400);font-size:13px">No response event data — email may not have been scored during onboarding.</div>`)}

            ${traceStage("Stage 3: Classification", !!cls, cls ? `
                <div class="em-kv-grid">
                    <div class="em-kv-label">Needs Response</div>
                    <div class="em-kv-value">${cls.needs_response ? '<span class="em-badge em-badge-amber">Yes</span>' : '<span class="em-badge em-badge-slate">No</span>'}</div>
                    <div class="em-kv-label">Confidence</div>
                    <div class="em-kv-value">${cls.confidence != null ? (cls.confidence * 100).toFixed(0) + "%" : "—"}</div>
                    <div class="em-kv-label">Archetype</div>
                    <div class="em-kv-value">${escapeHtml(cls.archetype || "—")}</div>
                    <div class="em-kv-label">Reason</div>
                    <div class="em-kv-value">${escapeHtml(cls.reason || cls.context || "—")}</div>
                    <div class="em-kv-label">Action</div>
                    <div class="em-kv-value">${escapeHtml(cls.action || "—")}</div>
                    <div class="em-kv-label">Project</div>
                    <div class="em-kv-value">${escapeHtml(cls.project || "—")}</div>
                    <div class="em-kv-label">Priority</div>
                    <div class="em-kv-value">${cls.priority === "x" ? '<span class="em-badge em-badge-red">Urgent</span>' : "Normal"}</div>
                </div>
            ` : `<div style="color:var(--em-slate-400);font-size:13px">Email not yet classified.</div>`)}

            ${traceStage("Stage 4: Draft", !!draft, draft ? `
                <div style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:var(--em-slate-700)">${escapeHtml(draft.draft_body)}</div>
                <div style="margin-top:8px;font-size:11px;color:var(--em-slate-400)">
                    ${draft.user_edited ? "Edited by user" : "Auto-generated"}
                </div>
            ` : `<div style="color:var(--em-slate-400);font-size:13px">No draft generated.</div>`, true)}
        </div>
    `;
}

function traceStage(label, hasData, content, isLast = false) {
    return `
        <div class="em-trace-stage${isLast ? " last" : ""}">
            <div class="em-trace-dot${hasData ? "" : " em-trace-dot-empty"}"></div>
            <div class="em-trace-label">${label}</div>
            ${content}
        </div>
    `;
}
