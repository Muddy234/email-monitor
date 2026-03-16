/**
 * Dashboard page — status banner, actionable cards, metrics, weekly summary.
 * Answers "what do I need to deal with right now?" first, then provides context.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, showEmpty, getParam, setParam, relativeTime, escapeHtml } from "../ui.js";
import { requireSubscription, isGrandfathered, openPortal } from "../subscription.js";

await requireAuth();
listenAuthChanges();
await renderNav();

const subscription = await requireSubscription();
if (!subscription) throw new Error("subscription_required");

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let range = parseInt(getParam("range", "7"), 10);

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const statusBannerEl = document.getElementById("statusBanner");
const actionableCardsEl = document.getElementById("actionableCards");
const metricsGrid = document.getElementById("metricsGrid");
const weeklySummaryEl = document.getElementById("weeklySummary");
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
        loadDashboard();
    });
});

syncFilterUI();

// -------------------------------------------------------------------------
// Load all dashboard data
// -------------------------------------------------------------------------

async function loadDashboard() {
    const start = rangeStart();

    try {
        const [allEmailsRes, rangeEmailsRes, eventsRes, runsRes] = await Promise.all([
            supabase
                .from("emails")
                .select("id, status, sender, sender_name, sender_email, subject, received_time, classifications(needs_response, action, context), drafts(id, draft_body)")
                .not("status", "in", "(completed,dismissed)")
                .order("received_time", { ascending: false }),
            supabase
                .from("emails")
                .select("id", { count: "exact", head: true })
                .gte("created_at", start),
            supabase
                .from("response_events")
                .select("email_id, pri, mc, sender_tier, rt, reason"),
            supabase
                .from("pipeline_runs")
                .select("emails_processed, drafts_generated")
                .gte("started_at", start),
        ]);

        if (allEmailsRes.error) throw allEmailsRes.error;

        const emails = allEmailsRes.data || [];
        const emailCount = rangeEmailsRes.count || 0;

        // Index response events
        const evMap = {};
        if (eventsRes.data) {
            for (const ev of eventsRes.data) evMap[ev.email_id] = ev;
        }

        // Categorize actionable emails
        const drafts = [];
        const notable = [];

        for (const email of emails) {
            const hasDraft = email.drafts && email.drafts.length > 0;
            if (hasDraft) {
                drafts.push(email);
            } else {
                const cls = email.classifications?.[0];
                if (!cls || cls.needs_response) continue;
                const ev = evMap[email.id];
                if (!ev) continue;
                if (ev.pri === "high" || ev.pri === "med" || ev.mc === true ||
                    ev.sender_tier === "C" || ev.sender_tier === "I" || ev.rt !== "none") {
                    notable.push(email);
                }
            }
        }

        // Aggregate pipeline stats
        let processed = 0, generated = 0;
        if (runsRes.data) {
            for (const row of runsRes.data) {
                processed += row.emails_processed || 0;
                generated += row.drafts_generated || 0;
            }
        }

        renderStatusBanner(drafts.length, notable.length, emailCount);
        renderActionableCards(drafts, notable, evMap);
        renderMetrics(emailCount, drafts.length, processed);
        renderWeeklySummary(emailCount, generated, drafts.length + notable.length);
    } catch (err) {
        showError(`Failed to load dashboard: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Status Banner
// -------------------------------------------------------------------------

function renderStatusBanner(draftCount, notableCount, emailCount) {
    const parts = [];

    if (draftCount > 0) {
        parts.push(`<a href="/app/emails.html?tab=drafts" class="em-status-link"><strong>${draftCount} draft${draftCount === 1 ? "" : "s"}</strong> to review</a>`);
    }
    if (notableCount > 0) {
        parts.push(`<a href="/app/emails.html?tab=notable" class="em-status-link"><strong>${notableCount} email${notableCount === 1 ? "" : "s"}</strong> worth reading</a>`);
    }

    let message;
    if (parts.length > 0) {
        message = `You have ${parts.join(" and ")}`;
    } else {
        const rangeLabel = range <= 1 ? "today" : `this week`;
        message = `All caught up — Clarion processed <strong>${emailCount}</strong> emails ${rangeLabel}`;
    }

    statusBannerEl.innerHTML = `
        <div class="em-status-banner">
            <div class="em-status-banner-text">${message}</div>
        </div>
    `;
}

// -------------------------------------------------------------------------
// Actionable Email Cards
// -------------------------------------------------------------------------

function renderActionableCards(drafts, notable, evMap) {
    const actionable = [...drafts, ...notable].slice(0, 8);

    if (actionable.length === 0) {
        actionableCardsEl.innerHTML = "";
        return;
    }

    const overflow = drafts.length + notable.length - 8;

    let html = `<div class="em-actionable-section">
        <div class="em-section-title">Needs Your Attention</div>
        <div class="em-actionable-list">`;

    for (const email of actionable) {
        const hasDraft = email.drafts && email.drafts.length > 0;
        const ev = evMap[email.id];
        const cls = email.classifications?.[0];

        let priorityBadge = "";
        if (ev?.pri === "high") {
            priorityBadge = `<span class="em-badge em-badge-red">High</span>`;
        } else if (ev?.pri === "med") {
            priorityBadge = `<span class="em-badge em-badge-amber">Med</span>`;
        }

        const action = ev?.reason || cls?.action || cls?.context || "";
        const tabParam = hasDraft ? "drafts" : "notable";

        html += `
            <a href="/app/emails.html?tab=${tabParam}" class="em-actionable-card">
                <div class="em-actionable-card-header">
                    <span class="em-actionable-sender">${escapeHtml(email.sender_name || email.sender || "Unknown")}</span>
                    <span class="em-actionable-meta">
                        ${priorityBadge}
                        ${hasDraft ? `<span class="em-badge em-badge-blue">Draft</span>` : ""}
                        <span class="em-actionable-time">${relativeTime(email.received_time)}</span>
                    </span>
                </div>
                <div class="em-actionable-subject">${escapeHtml(email.subject || "(no subject)")}</div>
                ${action ? `<div class="em-actionable-action">${escapeHtml(action)}</div>` : ""}
            </a>
        `;
    }

    html += `</div>`;

    if (overflow > 0) {
        html += `<a href="/app/emails.html" class="em-actionable-more">View all on Emails page →</a>`;
    }

    html += `</div>`;

    actionableCardsEl.innerHTML = html;
}

// -------------------------------------------------------------------------
// Metrics
// -------------------------------------------------------------------------

function renderMetrics(emailCount, draftCount, processedCount) {
    const rangeLabel = range <= 1 ? "today" : `last ${range} days`;

    metricsGrid.innerHTML = `
        <a href="/app/emails.html?tab=drafts" class="em-metric-card em-metric-link">
            <div class="em-metric-label">Drafts Ready</div>
            <div class="em-metric-value">${draftCount}</div>
            <div class="em-metric-context">${draftCount === 0 ? "All reviewed" : `${draftCount} ready to send`}</div>
        </a>
        <div class="em-metric-card">
            <div class="em-metric-label">Emails Processed</div>
            <div class="em-metric-value">${processedCount}</div>
            <div class="em-metric-context">${rangeLabel}</div>
        </div>
        <a href="/app/emails.html" class="em-metric-card em-metric-link">
            <div class="em-metric-label">Emails Synced</div>
            <div class="em-metric-value">${emailCount}</div>
            <div class="em-metric-context">${rangeLabel}</div>
        </a>
    `;
}

// -------------------------------------------------------------------------
// Weekly Summary
// -------------------------------------------------------------------------

function renderWeeklySummary(emailCount, draftsGenerated, actionableCount) {
    const rangeLabel = range <= 1 ? "Today" : `This ${range === 7 ? "week" : "month"}`;

    weeklySummaryEl.innerHTML = `
        <div class="em-card em-weekly-summary">
            <div class="em-weekly-summary-text">
                ${rangeLabel}: Clarion processed <strong>${emailCount}</strong> emails,
                drafted <strong>${draftsGenerated}</strong> responses${actionableCount > 0
                    ? `, <strong>${actionableCount}</strong> still need your attention`
                    : ""}.
            </div>
        </div>
    `;
}

// -------------------------------------------------------------------------
// Billing card
// -------------------------------------------------------------------------

function renderBillingCard() {
    const container = document.getElementById("billingSection");
    if (!container) return;

    if (isGrandfathered(subscription)) {
        container.innerHTML = `
            <div class="em-card em-billing-card">
                <div class="em-billing-info">
                    <div class="em-billing-label">Subscription</div>
                    <div class="em-billing-plan">Clarion AI Pro</div>
                    <div class="em-billing-grandfathered">Grandfathered &mdash; no renewal</div>
                </div>
            </div>
        `;
        return;
    }

    let detail = "";
    if (subscription.cancel_at_period_end && subscription.current_period_end) {
        const endDate = new Date(subscription.current_period_end).toLocaleDateString("en-US", {
            year: "numeric", month: "long", day: "numeric",
        });
        detail = `Cancels on ${endDate}`;
    } else if (subscription.current_period_end) {
        const endDate = new Date(subscription.current_period_end).toLocaleDateString("en-US", {
            year: "numeric", month: "long", day: "numeric",
        });
        detail = `Renews ${endDate}`;
    }

    const statusLabel = subscription.status === "past_due" ? "Past Due"
        : subscription.status === "trialing" ? "Trial"
        : "Active";

    container.innerHTML = `
        <div class="em-card em-billing-card">
            <div class="em-billing-info">
                <div class="em-billing-label">Subscription</div>
                <div class="em-billing-plan">Clarion AI Pro &mdash; ${escapeHtml(statusLabel)}</div>
                ${detail ? `<div class="em-billing-detail">${escapeHtml(detail)}</div>` : ""}
            </div>
            <button class="em-btn em-btn-secondary" id="manageBillingBtn">Manage Subscription</button>
        </div>
    `;

    document.getElementById("manageBillingBtn")?.addEventListener("click", async () => {
        const btn = document.getElementById("manageBillingBtn");
        btn.disabled = true;
        btn.textContent = "Redirecting...";
        try {
            const result = await openPortal();
            window.location.href = result.url;
        } catch (err) {
            btn.disabled = false;
            btn.textContent = "Manage Subscription";
        }
    });
}

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

loadDashboard();
renderBillingCard();
