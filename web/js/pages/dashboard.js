/**
 * Dashboard page — metrics overview with date range filter.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, getParam, setParam, formatDate, relativeTime } from "../ui.js";

await requireAuth();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let range = parseInt(getParam("range", "7"), 10);

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const metricsGrid = document.getElementById("metricsGrid");
const latestRunEl = document.getElementById("latestRun");
const filterBtns = document.querySelectorAll(".em-filter-btn[data-range]");
const refreshBtn = document.getElementById("refreshBtn");

// -------------------------------------------------------------------------
// Date range helpers
// -------------------------------------------------------------------------

function rangeStart() {
    const d = new Date();
    d.setDate(d.getDate() - range);
    d.setHours(0, 0, 0, 0);
    return d.toISOString();
}

// -------------------------------------------------------------------------
// Filter buttons
// -------------------------------------------------------------------------

function syncFilterUI() {
    filterBtns.forEach(btn => {
        btn.classList.toggle("active", parseInt(btn.dataset.range, 10) === range);
    });
}

filterBtns.forEach(btn => {
    btn.addEventListener("click", () => {
        range = parseInt(btn.dataset.range, 10);
        setParam("range", String(range));
        syncFilterUI();
        loadMetrics();
    });
});

refreshBtn.addEventListener("click", () => {
    loadMetrics();
    loadLatestRun();
});

syncFilterUI();

// -------------------------------------------------------------------------
// Load metrics
// -------------------------------------------------------------------------

async function loadMetrics() {
    const start = rangeStart();

    try {
        const [emailsRes, needsResponseRes, draftsRes, runsRes] = await Promise.all([
            supabase.from("emails").select("*", { count: "exact", head: true }).gte("created_at", start),
            supabase.from("classifications").select("*", { count: "exact", head: true }).eq("needs_response", true).gte("created_at", start),
            supabase.from("drafts").select("*", { count: "exact", head: true }).gte("created_at", start),
            supabase.from("pipeline_runs").select("emails_scanned, emails_processed, drafts_generated").gte("started_at", start),
        ]);

        const emailCount = emailsRes.count || 0;
        const needsResponseCount = needsResponseRes.count || 0;
        const draftCount = draftsRes.count || 0;

        // Aggregate pipeline funnel
        let scanned = 0, processed = 0, generated = 0;
        if (runsRes.data) {
            for (const row of runsRes.data) {
                scanned += row.emails_scanned || 0;
                processed += row.emails_processed || 0;
                generated += row.drafts_generated || 0;
            }
        }

        if (emailCount === 0 && draftCount === 0 && scanned === 0) {
            showEmpty(metricsGrid, "No data yet \u2014 run the extension to sync emails.");
            return;
        }

        metricsGrid.innerHTML = `
            <div class="em-metric-card">
                <div class="em-metric-label">Emails Synced</div>
                <div class="em-metric-value">${emailCount}</div>
            </div>
            <div class="em-metric-card">
                <div class="em-metric-label">Needs Response</div>
                <div class="em-metric-value">${needsResponseCount}</div>
            </div>
            <div class="em-metric-card">
                <div class="em-metric-label">Drafts Generated</div>
                <div class="em-metric-value">${draftCount}</div>
            </div>
            <div class="em-metric-card">
                <div class="em-metric-label">Pipeline Funnel</div>
                <div style="font-size: 13px; color: var(--em-slate-700); margin-top: 8px;">
                    <div>${scanned} scanned</div>
                    <div>${processed} processed</div>
                    <div>${generated} drafts</div>
                </div>
            </div>
        `;
    } catch (err) {
        showError(`Failed to load metrics: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Load latest pipeline run
// -------------------------------------------------------------------------

async function loadLatestRun() {
    try {
        const { data, error } = await supabase
            .from("pipeline_runs")
            .select("*")
            .order("started_at", { ascending: false })
            .limit(1)
            .maybeSingle();

        if (error) throw error;

        if (!data) {
            latestRunEl.innerHTML = `<div class="em-empty" style="padding: 24px;">No pipeline runs recorded yet.</div>`;
            return;
        }

        const statusBadge = data.status === "completed"
            ? `<span class="em-badge em-badge-green">completed</span>`
            : data.status === "failed"
            ? `<span class="em-badge em-badge-red">failed</span>`
            : `<span class="em-badge em-badge-amber">${data.status}</span>`;

        latestRunEl.innerHTML = `
            <div style="font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--em-slate-500); margin-bottom: 12px;">Latest Pipeline Run</div>
            <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
                ${statusBadge}
                <span style="font-size: 13px; color: var(--em-slate-700);">${formatDate(data.started_at)}</span>
                <span style="font-size: 13px; color: var(--em-slate-500);">${data.emails_scanned || 0} scanned, ${data.emails_processed || 0} processed, ${data.drafts_generated || 0} drafts</span>
            </div>
            ${data.error_message ? `<div style="margin-top: 8px; font-size: 12px; color: var(--em-red-600);">${data.error_message}</div>` : ""}
        `;
    } catch (err) {
        showError(`Failed to load latest run: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadMetrics();
loadLatestRun();
