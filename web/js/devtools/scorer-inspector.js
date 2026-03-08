/**
 * Panel 3: Scorer Inspector
 * Select an email → see response_event features, scoring params, classification.
 */
import { supabase } from "../supabase-client.js";
import { escapeHtml } from "../ui.js";
import { createEmailPicker } from "./email-picker.js";

let cachedScoring = null;

export async function initScorerInspector() {
    const panel = document.getElementById("panel-scorer");

    const { data: { user } } = await supabase.auth.getUser();
    if (!user) { panel.innerHTML = `<div class="em-empty">Not authenticated.</div>`; return; }

    // Pre-fetch scoring params
    const { data: scoring } = await supabase
        .from("scoring_parameters")
        .select("*")
        .eq("user_id", user.id)
        .single();
    cachedScoring = scoring;

    panel.innerHTML = `
        <div class="em-detail-grid">
            <div>
                <h3 class="em-section-title">Select Email</h3>
                <div id="dt-scorer-picker"></div>
            </div>
            <div>
                <h3 class="em-section-title">Scoring Model</h3>
                <div class="em-card" id="dt-scorer-params"></div>
            </div>
        </div>
        <div id="dt-scorer-detail"></div>
    `;

    renderScoringParams(scoring);

    createEmailPicker(
        document.getElementById("dt-scorer-picker"),
        (email) => renderScorerDetail(email, user.id)
    );
}

function renderScoringParams(scoring) {
    const el = document.getElementById("dt-scorer-params");
    if (!scoring) {
        el.innerHTML = `<div class="em-empty" style="padding:16px">No scoring parameters found.</div>`;
        return;
    }

    const params = scoring.parameters || {};
    const lift = params.lift_factors || {};
    const boolLifts = lift.boolean || {};

    const liftRows = Object.entries(boolLifts)
        .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
        .map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${v > 0 ? "+" : ""}${v.toFixed(3)}</td></tr>`)
        .join("");

    el.innerHTML = `
        <div style="font-size:12px;font-weight:600;color:var(--em-slate-500);margin-bottom:8px">Boolean Lift Factors</div>
        <div class="em-table-wrap" style="max-height:220px;overflow-y:auto">
            <table class="em-table">
                <thead><tr><th>Feature</th><th>Lift</th></tr></thead>
                <tbody>${liftRows || '<tr><td colspan="2" style="color:var(--em-slate-400)">None</td></tr>'}</tbody>
            </table>
        </div>
    `;
}

async function renderScorerDetail(email, userId) {
    const detail = document.getElementById("dt-scorer-detail");

    // Fetch response_event for this email
    const { data: events } = await supabase
        .from("response_events")
        .select("*")
        .eq("user_id", userId)
        .eq("email_id", email.id)
        .limit(1);

    const evt = events?.[0];
    const cls = email.classifications?.[0];

    detail.innerHTML = `
        <div class="em-detail-grid" style="margin-top:24px">
            <div>
                <h3 class="em-section-title">Response Event Features</h3>
                <div class="em-card">
                    ${evt ? `
                        <div class="em-kv-grid">
                            <div class="em-kv-label">Responded</div>
                            <div class="em-kv-value">${evt.responded ? '<span class="em-badge em-badge-green">Yes</span>' : '<span class="em-badge em-badge-slate">No</span>'}</div>
                            <div class="em-kv-label">Response Latency</div>
                            <div class="em-kv-value">${evt.response_latency_hours != null ? evt.response_latency_hours.toFixed(1) + " hours" : "—"}</div>
                            <div class="em-kv-label">Has Question</div>
                            <div class="em-kv-value">${fmtBool(evt.has_question)}</div>
                            <div class="em-kv-label">Has Action Language</div>
                            <div class="em-kv-value">${fmtBool(evt.has_action_language)}</div>
                            <div class="em-kv-label">Subject Type</div>
                            <div class="em-kv-value">${escapeHtml(evt.subject_type || "—")}</div>
                            <div class="em-kv-label">Response Type</div>
                            <div class="em-kv-value">${escapeHtml(evt.response_type || "—")}</div>
                            <div class="em-kv-label">User Position</div>
                            <div class="em-kv-value">${escapeHtml(evt.user_position || "—")}</div>
                            <div class="em-kv-label">Total Recipients</div>
                            <div class="em-kv-value">${evt.total_recipients ?? "—"}</div>
                            <div class="em-kv-label">Is Recurring</div>
                            <div class="em-kv-value">${fmtBool(evt.is_recurring)}</div>
                            <div class="em-kv-label">Sender</div>
                            <div class="em-kv-value">${escapeHtml(evt.sender_email || "—")}</div>
                        </div>
                    ` : `<div class="em-empty" style="padding:16px">No response event data for this email.</div>`}
                </div>
            </div>
            <div>
                <h3 class="em-section-title">Classification Result</h3>
                <div class="em-card">
                    ${cls ? `
                        <div class="em-kv-grid">
                            <div class="em-kv-label">Needs Response</div>
                            <div class="em-kv-value">${cls.needs_response ? '<span class="em-badge em-badge-amber">Yes</span>' : '<span class="em-badge em-badge-slate">No</span>'}</div>
                            <div class="em-kv-label">Confidence</div>
                            <div class="em-kv-value">${cls.confidence != null ? (cls.confidence * 100).toFixed(0) + "%" : "—"}</div>
                            <div class="em-kv-label">Reason</div>
                            <div class="em-kv-value">${escapeHtml(cls.reason || cls.context || "—")}</div>
                            <div class="em-kv-label">Archetype</div>
                            <div class="em-kv-value">${escapeHtml(cls.archetype || "—")}</div>
                            <div class="em-kv-label">Action</div>
                            <div class="em-kv-value">${escapeHtml(cls.action || "—")}</div>
                            <div class="em-kv-label">Project</div>
                            <div class="em-kv-value">${escapeHtml(cls.project || "—")}</div>
                        </div>
                    ` : `<div class="em-empty" style="padding:16px">Not classified yet.</div>`}
                </div>
            </div>
        </div>
    `;
}

function fmtBool(val) {
    if (val === true) return '<span class="em-badge em-badge-green">Yes</span>';
    if (val === false) return '<span class="em-badge em-badge-slate">No</span>';
    return "—";
}
