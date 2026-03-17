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
import { createEmailPicker, clearEmailCache } from "./email-picker.js";
import {
    renderStage1,
    renderStage2,
    renderStage3_signals,
    renderStage4_context,
    renderStage5_draft,
    renderStage6_delivery,
} from "../components/trace-renderers.js";

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
