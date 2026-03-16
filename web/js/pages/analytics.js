/**
 * Analytics page — email volume charts, drafts generated, top senders.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, getParam, setParam, escapeHtml } from "../ui.js";
import { renderBarChart } from "../components/charts.js";

await requireAuth();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let range = parseInt(getParam("range", "30"), 10);

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const volumeChartBody = document.getElementById("volumeChartBody");
const draftsChartBody = document.getElementById("draftsChartBody");
const summaryStats = document.getElementById("summaryStats");
const topSendersBody = document.getElementById("topSendersBody");
const filterBtns = document.querySelectorAll(".em-filter-btn[data-range]");

// -------------------------------------------------------------------------
// Date range
// -------------------------------------------------------------------------

function rangeStart() {
    const d = new Date();
    d.setDate(d.getDate() - range);
    d.setHours(0, 0, 0, 0);
    return d.toISOString();
}

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
        loadAnalytics();
    });
});

syncFilterUI();

// -------------------------------------------------------------------------
// Load data
// -------------------------------------------------------------------------

async function loadAnalytics() {
    const start = rangeStart();

    try {
        const [emailsRes, runsRes, topSendersRes] = await Promise.all([
            supabase
                .from("emails")
                .select("id, received_time, sender, sender_name, sender_email")
                .gte("received_time", start)
                .order("received_time", { ascending: true }),
            supabase
                .from("pipeline_runs")
                .select("started_at, emails_processed, drafts_generated")
                .gte("started_at", start)
                .order("started_at", { ascending: true }),
            supabase
                .from("emails")
                .select("sender_email, sender_name")
                .gte("received_time", start),
        ]);

        if (emailsRes.error) throw emailsRes.error;

        const emails = emailsRes.data || [];
        const runs = runsRes.data || [];
        const senderEmails = topSendersRes.data || [];

        renderVolumeChart(emails);
        renderDraftsChart(runs);
        renderSummaryStats(emails, runs);
        renderTopSenders(senderEmails);
    } catch (err) {
        showError(`Failed to load analytics: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Volume chart
// -------------------------------------------------------------------------

function renderVolumeChart(emails) {
    const buckets = bucketByDay(emails, "received_time");
    renderBarChart(volumeChartBody, buckets, { color: "#2C4F7C" });
}

// -------------------------------------------------------------------------
// Drafts chart
// -------------------------------------------------------------------------

function renderDraftsChart(runs) {
    // Aggregate drafts_generated per day
    const dayMap = {};
    for (const run of runs) {
        const day = new Date(run.started_at).toLocaleDateString("en-US", { month: "short", day: "numeric" });
        dayMap[day] = (dayMap[day] || 0) + (run.drafts_generated || 0);
    }

    // Fill in all days in range
    const data = [];
    const now = new Date();
    for (let i = range - 1; i >= 0; i--) {
        const d = new Date(now);
        d.setDate(d.getDate() - i);
        const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
        data.push({ label, value: dayMap[label] || 0 });
    }

    renderBarChart(draftsChartBody, data, { color: "#10B981" });
}

// -------------------------------------------------------------------------
// Summary stats
// -------------------------------------------------------------------------

function renderSummaryStats(emails, runs) {
    let totalProcessed = 0;
    let totalDrafts = 0;
    for (const run of runs) {
        totalProcessed += run.emails_processed || 0;
        totalDrafts += run.drafts_generated || 0;
    }

    const avgPerDay = range > 0 ? Math.round(emails.length / range) : 0;
    const draftRate = emails.length > 0 ? Math.round((totalDrafts / emails.length) * 100) : 0;

    summaryStats.innerHTML = `
        <div class="em-metric-card">
            <div class="em-metric-label">Emails Received</div>
            <div class="em-metric-value">${emails.length}</div>
            <div class="em-metric-context">last ${range} days</div>
        </div>
        <div class="em-metric-card">
            <div class="em-metric-label">Avg per Day</div>
            <div class="em-metric-value">${avgPerDay}</div>
            <div class="em-metric-context">emails/day</div>
        </div>
        <div class="em-metric-card">
            <div class="em-metric-label">Drafts Generated</div>
            <div class="em-metric-value">${totalDrafts}</div>
            <div class="em-metric-context">${draftRate}% draft rate</div>
        </div>
    `;
}

// -------------------------------------------------------------------------
// Top senders
// -------------------------------------------------------------------------

function renderTopSenders(emails) {
    // Count by sender_email
    const counts = {};
    const names = {};
    for (const e of emails) {
        const key = (e.sender_email || "").toLowerCase();
        if (!key) continue;
        counts[key] = (counts[key] || 0) + 1;
        if (e.sender_name && !names[key]) names[key] = e.sender_name;
    }

    const sorted = Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 10);

    if (sorted.length === 0) {
        topSendersBody.innerHTML = `<div class="em-chart-empty">No sender data for this period</div>`;
        return;
    }

    let html = `<table class="em-table">
        <thead><tr><th>Sender</th><th>Emails</th></tr></thead>
        <tbody>`;

    for (const [email, count] of sorted) {
        const displayName = names[email] || email;
        html += `<tr>
            <td>
                <div class="em-sender-cell">
                    <span class="em-sender-name">${escapeHtml(displayName)}</span>
                    <span class="em-sender-email">${escapeHtml(email)}</span>
                </div>
            </td>
            <td class="em-table-num">${count}</td>
        </tr>`;
    }

    html += `</tbody></table>`;
    topSendersBody.innerHTML = html;
}

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

function bucketByDay(items, dateField) {
    const dayMap = {};
    for (const item of items) {
        const day = new Date(item[dateField]).toLocaleDateString("en-US", { month: "short", day: "numeric" });
        dayMap[day] = (dayMap[day] || 0) + 1;
    }

    const data = [];
    const now = new Date();
    for (let i = range - 1; i >= 0; i--) {
        const d = new Date(now);
        d.setDate(d.getDate() - i);
        const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
        data.push({ label, value: dayMap[label] || 0 });
    }
    return data;
}

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadAnalytics();
