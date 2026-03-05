/**
 * Dashboard page — metrics overview with pipeline viz and CTAs.
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
const pipelineSection = document.getElementById("pipelineSection");
const ctaSection = document.getElementById("ctaSection");
const latestRunEl = document.getElementById("latestRun");
const greetingEl = document.getElementById("greeting");
const filterBtns = document.querySelectorAll(".em-filter-btn[data-range]");

// -------------------------------------------------------------------------
// Greeting
// -------------------------------------------------------------------------

function setGreeting() {
    const hour = new Date().getHours();
    let greeting = "Good evening";
    if (hour < 12) greeting = "Good morning";
    else if (hour < 17) greeting = "Good afternoon";
    greetingEl.textContent = `${greeting}. Here's your overview.`;
}

setGreeting();

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

syncFilterUI();

// -------------------------------------------------------------------------
// Load metrics
// -------------------------------------------------------------------------

async function loadMetrics() {
    const start = rangeStart();

    try {
        // Three parallel queries:
        // 1. ALL emails (no date filter) — for actionable counts (Needs Response, Drafts Ready)
        // 2. Date-filtered emails — for "Emails Synced" volume metric
        // 3. Pipeline runs — for funnel stats
        const [allEmailsRes, rangeEmailsRes, runsRes] = await Promise.all([
            supabase
                .from("emails")
                .select("id, status, classifications(needs_response), drafts(id)"),
            supabase
                .from("emails")
                .select("id", { count: "exact", head: true })
                .gte("created_at", start),
            supabase
                .from("pipeline_runs")
                .select("emails_scanned, emails_processed, drafts_generated")
                .gte("started_at", start),
        ]);

        if (allEmailsRes.error) throw allEmailsRes.error;

        const emails = allEmailsRes.data || [];
        const emailCount = rangeEmailsRes.count || 0;

        // Count using same grouping logic as emails page (no date filter)
        let needsResponseCount = 0;
        let draftCount = 0;
        for (const e of emails) {
            if (e.status === "completed") continue;
            const hasDraft = e.drafts && e.drafts.length > 0;
            const needsResponse = e.classifications?.some(c => c.needs_response);
            if (hasDraft) draftCount++;
            else if (needsResponse) needsResponseCount++;
        }

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
            pipelineSection.innerHTML = "";
            ctaSection.innerHTML = "";
            return;
        }

        renderMetrics(emailCount, needsResponseCount, draftCount);
        renderPipeline(scanned, processed, generated);
        renderCTA(needsResponseCount, draftCount);
    } catch (err) {
        showError(`Failed to load metrics: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Render metric cards
// -------------------------------------------------------------------------

function renderMetrics(emailCount, needsResponseCount, draftCount) {
    const contextEmails = emailCount === 0 ? "Nothing synced yet" : `${emailCount} in the last ${range} day${range > 1 ? "s" : ""}`;
    const contextNeeds = needsResponseCount === 0 ? "All caught up" : `${needsResponseCount} awaiting your input`;
    const contextDrafts = draftCount === 0 ? "No drafts yet" : `${draftCount} ready to review`;

    metricsGrid.innerHTML = `
        <a href="/app/emails.html" class="em-metric-card em-metric-link">
            <div class="em-metric-label">Emails Synced</div>
            <div class="em-metric-value">${emailCount}</div>
            <div class="em-metric-context">${contextEmails}</div>
        </a>
        <a href="/app/emails.html?section=needs-response" class="em-metric-card em-metric-link">
            <div class="em-metric-label">Needs Response</div>
            <div class="em-metric-value">${needsResponseCount}</div>
            <div class="em-metric-context">${contextNeeds}</div>
        </a>
        <a href="/app/emails.html?section=drafts" class="em-metric-card em-metric-link">
            <div class="em-metric-label">Drafts Ready</div>
            <div class="em-metric-value">${draftCount}</div>
            <div class="em-metric-context">${contextDrafts}</div>
        </a>
    `;
}

// -------------------------------------------------------------------------
// Pipeline visualization
// -------------------------------------------------------------------------

function renderPipeline(scanned, processed, generated) {
    if (scanned === 0 && processed === 0 && generated === 0) {
        pipelineSection.innerHTML = "";
        return;
    }

    pipelineSection.innerHTML = `
        <div class="em-pipeline">
            <div class="em-pipeline-stage">
                <div class="em-pipeline-stage-value">${scanned}</div>
                <div class="em-pipeline-stage-label">Scanned</div>
            </div>
            <div class="em-pipeline-stage">
                <div class="em-pipeline-stage-value">${processed}</div>
                <div class="em-pipeline-stage-label">Classified</div>
            </div>
            <div class="em-pipeline-stage">
                <div class="em-pipeline-stage-value">${generated}</div>
                <div class="em-pipeline-stage-label">Drafts Created</div>
            </div>
        </div>
    `;
}

// -------------------------------------------------------------------------
// CTA section
// -------------------------------------------------------------------------

function renderCTA(needsResponseCount, draftCount) {
    const cards = [];

    if (draftCount > 0) {
        cards.push(`
            <a href="/app/emails.html?section=drafts" class="em-cta-card">
                <div class="em-cta-title">Review ${draftCount} draft${draftCount > 1 ? "s" : ""}</div>
                <div class="em-cta-desc">Drafts are ready for your review before sending.</div>
            </a>
        `);
    }

    if (needsResponseCount > 0) {
        cards.push(`
            <a href="/app/emails.html?section=needs-response" class="em-cta-card">
                <div class="em-cta-title">${needsResponseCount} email${needsResponseCount > 1 ? "s" : ""} need attention</div>
                <div class="em-cta-desc">These emails were flagged as needing a response.</div>
            </a>
        `);
    }

    if (cards.length === 0) {
        ctaSection.innerHTML = "";
        return;
    }

    ctaSection.innerHTML = `<div class="em-cta-row">${cards.join("")}</div>`;
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

        const statusClass = data.status === "completed"
            ? "em-badge-green"
            : data.status === "failed"
            ? "em-badge-red"
            : "em-badge-amber";

        latestRunEl.innerHTML = `
            <div class="em-latest-run-header">Latest Pipeline Run</div>
            <div class="em-latest-run-body">
                <span class="em-badge ${statusClass}">${data.status}</span>
                <span class="em-latest-run-time">${relativeTime(data.started_at)}</span>
                <span class="em-latest-run-stats">${data.emails_scanned || 0} scanned, ${data.emails_processed || 0} processed, ${data.drafts_generated || 0} drafts</span>
            </div>
            ${data.error_message ? `<div class="em-latest-run-error">${data.error_message}</div>` : ""}
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
