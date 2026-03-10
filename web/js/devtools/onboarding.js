/**
 * Panel 1: Onboarding Artifacts
 * Displays style guide, contact profiles, topic domains, scoring summary.
 */
import { supabase } from "../supabase-client.js";
import { escapeHtml, formatDate } from "../ui.js";

export async function initOnboarding() {
    const panel = document.getElementById("panel-onboarding");
    panel.innerHTML = `<div class="em-skeleton em-skeleton-card"></div>`;

    const { data: { user } } = await supabase.auth.getUser();
    if (!user) { panel.innerHTML = `<div class="em-empty">Not authenticated.</div>`; return; }

    const [profileRes, contactsRes, topicRes, scoringRes] = await Promise.all([
        supabase.from("profiles").select("writing_style_guide, style_sample_count, onboarding_status, onboarding_completed_at").eq("id", user.id).single(),
        supabase.from("contacts").select("*").eq("user_id", user.id).order("emails_per_month", { ascending: false }),
        supabase.from("user_topic_profile").select("*").eq("user_id", user.id).single(),
        supabase.from("scoring_parameters").select("*").eq("user_id", user.id).single(),
    ]);

    const profile = profileRes.data;
    const contacts = contactsRes.data || [];
    const topics = topicRes.data;
    const scoring = scoringRes.data;

    if (!profile?.onboarding_completed_at) {
        panel.innerHTML = `<div class="em-card"><div class="em-empty">Onboarding not yet completed.</div></div>`;
        return;
    }

    panel.innerHTML = `
        <div class="em-detail-grid">
            <div>
                <h3 class="em-section-title">Style Guide</h3>
                <div class="em-card" id="dt-style-guide"></div>
            </div>
            <div>
                <h3 class="em-section-title">Scoring Model</h3>
                <div class="em-card" id="dt-scoring"></div>
            </div>
        </div>

        <h3 class="em-section-title">Topic Domains</h3>
        <div class="em-card" id="dt-topics" style="margin-bottom:24px"></div>

        <h3 class="em-section-title">Contact Profiles <span class="em-group-count">${contacts.length}</span></h3>
        <div style="margin-bottom:12px">
            <input type="text" class="em-search" id="dt-contacts-search" placeholder="Search contacts..." />
        </div>
        <div class="em-card" style="padding:0; overflow:hidden">
            <div class="em-table-wrap" id="dt-contacts-table"></div>
        </div>
    `;

    // Style Guide
    renderStyleGuide(profile);

    // Scoring
    renderScoring(scoring, profile);

    // Topics
    renderTopics(topics);

    // Contacts
    renderContacts(contacts);
}

function renderStyleGuide(profile) {
    const el = document.getElementById("dt-style-guide");
    if (!profile.writing_style_guide) {
        el.innerHTML = `<div class="em-empty">No style guide generated.</div>`;
        return;
    }
    el.innerHTML = `
        <div style="margin-bottom:10px; display:flex; gap:12px; font-size:12px; color:var(--em-slate-500)">
            <span>Samples: ${profile.style_sample_count || 0}</span>
            <span>Completed: ${formatDate(profile.onboarding_completed_at)}</span>
        </div>
        <div class="em-style-guide-text">${escapeHtml(profile.writing_style_guide)}</div>
    `;
}

function renderScoring(scoring, profile) {
    const el = document.getElementById("dt-scoring");
    if (!scoring) {
        el.innerHTML = `<div class="em-empty">No scoring parameters found.</div>`;
        return;
    }
    const params = scoring.parameters || {};
    const meta = params.meta || {};
    const triage = params.triage || {};
    const breakpoints = params.iso_breakpoints || [];
    const recurring = params.recurring_patterns || {};

    el.innerHTML = `
        <div class="em-kv-grid">
            <div class="em-kv-label">Global Response Rate</div>
            <div class="em-kv-value">${meta.global_rate != null ? (meta.global_rate * 100).toFixed(1) + "%" : "—"}</div>
            <div class="em-kv-label">Training Events</div>
            <div class="em-kv-value">${meta.total_events || scoring.emails_used || "—"}</div>
            <div class="em-kv-label">Hard Gate Threshold</div>
            <div class="em-kv-value">${triage.hard_gate_threshold ?? "—"}</div>
            <div class="em-kv-label">Soft Gate Threshold</div>
            <div class="em-kv-value">${triage.soft_gate_threshold ?? "—"}</div>
            <div class="em-kv-label">Calibration Breakpoints</div>
            <div class="em-kv-value">${breakpoints.length}</div>
            <div class="em-kv-label">Recurring Patterns</div>
            <div class="em-kv-value">${Object.keys(recurring).length}</div>
            <div class="em-kv-label">Prior Weight</div>
            <div class="em-kv-value">${params.prior_weight ?? "—"}</div>
            <div class="em-kv-label">Generated</div>
            <div class="em-kv-value">${scoring.generated_at ? formatDate(scoring.generated_at) : "—"}</div>
        </div>
    `;
}

function renderTopics(topics) {
    const el = document.getElementById("dt-topics");
    if (!topics) {
        el.innerHTML = `<div class="em-empty">No topic profile found.</div>`;
        return;
    }

    const domains = topics.domains || [];
    const highSignal = new Set(topics.high_signal_keywords || []);

    let html = "";

    if (domains.length) {
        html += domains.map(d => {
            const keywords = (d.keywords || []).map(k =>
                `<span class="em-keyword-badge${highSignal.has(k) ? " em-keyword-badge-high" : ""}">${escapeHtml(k)}</span>`
            ).join("");
            return `<div class="em-domain-card">
                <div class="em-domain-name">${escapeHtml(d.name || d.domain || "Unnamed")}</div>
                ${d.description ? `<div style="font-size:13px;color:var(--em-slate-500);margin-bottom:8px">${escapeHtml(d.description)}</div>` : ""}
                <div>${keywords || '<span style="color:var(--em-slate-400);font-size:12px">No keywords</span>'}</div>
            </div>`;
        }).join("");
    }

    if (highSignal.size) {
        html += `<div style="margin-top:16px">
            <div style="font-size:12px;font-weight:600;color:var(--em-slate-500);margin-bottom:8px">High-Signal Keywords</div>
            <div>${[...highSignal].map(k => `<span class="em-keyword-badge em-keyword-badge-high">${escapeHtml(k)}</span>`).join("")}</div>
        </div>`;
    }

    el.innerHTML = html || `<div class="em-empty">No topic domains.</div>`;
}

function renderContacts(contacts) {
    const tableWrap = document.getElementById("dt-contacts-table");
    const searchInput = document.getElementById("dt-contacts-search");

    let sortCol = "emails_per_month";
    let sortAsc = false;

    function render(list) {
        const sorted = [...list].sort((a, b) => {
            let va = a[sortCol], vb = b[sortCol];
            if (typeof va === "string") va = va?.toLowerCase() || "";
            if (typeof vb === "string") vb = vb?.toLowerCase() || "";
            if (va == null) va = sortAsc ? Infinity : -Infinity;
            if (vb == null) vb = sortAsc ? Infinity : -Infinity;
            return sortAsc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1);
        });

        const cols = [
            { key: "name", label: "Name" },
            { key: "email", label: "Email" },
            { key: "organization", label: "Org" },
            { key: "role", label: "Role" },
            { key: "contact_type", label: "Type" },
            { key: "relationship_significance", label: "Significance" },
            { key: "emails_per_month", label: "Emails/mo" },
            { key: "response_rate", label: "Response %" },
        ];

        tableWrap.innerHTML = `<table class="em-table">
            <thead><tr>${cols.map(c => {
                const isSorted = sortCol === c.key;
                const cls = `em-sortable${isSorted ? (sortAsc ? " em-sort-asc" : " em-sort-desc") : ""}`;
                return `<th class="${cls}" data-col="${c.key}">${c.label}</th>`;
            }).join("")}</tr></thead>
            <tbody>${sorted.map(c => `<tr>
                <td>${escapeHtml(c.name || "—")}</td>
                <td>${escapeHtml(c.email || "—")}</td>
                <td>${escapeHtml(c.organization || "—")}</td>
                <td>${escapeHtml(c.role || "—")}</td>
                <td><span class="em-badge em-badge-slate">${escapeHtml(c.contact_type || "unknown")}</span></td>
                <td>${escapeHtml(c.relationship_significance || "—")}</td>
                <td>${c.emails_per_month ?? "—"}</td>
                <td>${c.response_rate != null ? c.response_rate + "%" : "—"}</td>
            </tr>`).join("")}</tbody>
        </table>`;
    }

    function filter(query) {
        if (!query) return contacts;
        const q = query.toLowerCase();
        return contacts.filter(c =>
            (c.name || "").toLowerCase().includes(q) ||
            (c.email || "").toLowerCase().includes(q) ||
            (c.organization || "").toLowerCase().includes(q)
        );
    }

    tableWrap.addEventListener("click", (evt) => {
        const th = evt.target.closest("th.em-sortable");
        if (!th) return;
        const col = th.dataset.col;
        if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
        render(filter(searchInput.value));
    });

    searchInput.addEventListener("input", () => render(filter(searchInput.value)));

    render(contacts);
}
