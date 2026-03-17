/**
 * Account page — subscription status, checkout, billing portal.
 */
import { requireAuth, listenAuthChanges, getUserEmail } from "../auth.js";
import { renderNav } from "../nav.js";
import { supabase } from "../supabase-client.js";
import { showError, escapeHtml } from "../ui.js";
import {
    getSubscription,
    isSubscriptionActive,
    isGrandfathered,
    isTrialExpired,
    getTrialDaysRemaining,
    startCheckout,
    openPortal,
    pollForActivation,
} from "../subscription.js";

await requireAuth();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// DOM refs
// -------------------------------------------------------------------------

const container = document.getElementById("accountContainer");

// -------------------------------------------------------------------------
// Handle checkout success redirect
// -------------------------------------------------------------------------

const params = new URLSearchParams(window.location.search);
if (params.get("checkout") === "success") {
    params.delete("checkout");
    const cleanUrl = `${window.location.pathname}${params.toString() ? "?" + params : ""}`;
    history.replaceState(null, "", cleanUrl);

    container.innerHTML = `
        <div class="em-account-card" style="text-align: center; padding: 48px 24px;">
            <div class="em-spinner"></div>
            <h2 style="margin-top: 16px; font-size: 18px; color: var(--em-slate-900);">Confirming your payment...</h2>
            <p style="font-size: 13px; color: var(--em-slate-500); margin-top: 8px;" id="checkoutHint">This should only take a moment.</p>
        </div>
    `;

    const sub = await pollForActivation(5, 2000);
    if (sub) {
        renderAccount(sub);
    } else {
        const hint = document.getElementById("checkoutHint");
        if (hint) hint.textContent = "Payment received. Your account is being activated — please refresh in a moment.";
    }
} else {
    loadAccount();
}

// -------------------------------------------------------------------------
// Load account data
// -------------------------------------------------------------------------

async function loadAccount() {
    try {
        const sub = await getSubscription();
        renderAccount(sub);
    } catch (err) {
        showError(`Failed to load account: ${err.message}`);
    }
}

// -------------------------------------------------------------------------
// Render
// -------------------------------------------------------------------------

async function renderAccount(sub) {
    const email = await getUserEmail();
    const active = isSubscriptionActive(sub);
    const grandfathered = isGrandfathered(sub);

    const trialExpired = isTrialExpired(sub);
    const trialDays = getTrialDaysRemaining(sub);

    let statusBadge, statusClass;
    if (trialExpired) {
        statusBadge = "Trial Expired";
        statusClass = "em-badge-red";
    } else if (active) {
        statusBadge = sub.status === "trialing" ? "Trial" : sub.status === "past_due" ? "Past Due" : "Active";
        statusClass = sub.status === "past_due" ? "em-badge-amber" : "em-badge-green";
    } else if (sub?.status === "canceled") {
        statusBadge = "Canceled";
        statusClass = "em-badge-red";
    } else {
        statusBadge = "Inactive";
        statusClass = "em-badge-slate";
    }

    let renewalText = "";
    if (active && sub.status === "trialing" && trialDays !== null) {
        const dayLabel = trialDays === 1 ? "day" : "days";
        renewalText = `${trialDays} ${dayLabel} remaining in free trial`;
    } else if (active && sub.current_period_end) {
        const endDate = new Date(sub.current_period_end).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
        renewalText = sub.cancel_at_period_end
            ? `Cancels on ${endDate}`
            : `Renews on ${endDate}`;
    } else if (grandfathered) {
        renewalText = "Grandfathered — free access";
    }

    let actionHtml;
    if (trialExpired || (!active && !grandfathered)) {
        const expiredMsg = trialExpired
            ? `<p style="font-size: 13px; color: var(--em-slate-500); margin-bottom: 12px;">Your 7-day trial has ended. Subscribe to continue using Clarion AI.</p>`
            : "";
        actionHtml = `
            <div class="em-account-subscribe">
                <div class="em-account-offer">
                    <h3>Subscribe to Clarion AI</h3>
                    ${expiredMsg}
                    <p>AI-powered email classification and draft generation for your Outlook inbox.</p>
                    <div class="em-account-price">$10 <span>/ month</span></div>
                </div>
                <button class="em-btn em-btn-primary" id="subscribeBtn">
                    ${sub?.status === "canceled" ? "Resubscribe" : "Subscribe Now"}
                </button>
                <p class="em-account-hint" id="subscribeHint"></p>
            </div>
        `;
    } else if (active && sub.status === "trialing") {
        actionHtml = `
            <button class="em-btn em-btn-primary" id="subscribeBtn">Subscribe Now</button>
            <p class="em-account-hint" id="subscribeHint"></p>
            <button class="em-btn em-btn-secondary" id="manageBtn" style="display:none">Manage Billing</button>
        `;
    } else {
        actionHtml = `
            <button class="em-btn em-btn-secondary" id="manageBtn">Manage Billing</button>
        `;
    }

    container.innerHTML = `
        <div class="em-account-card">
            <div class="em-account-header">
                <div class="em-account-avatar">${(email || "?")[0].toUpperCase()}</div>
                <div>
                    <div class="em-account-email">${escapeHtml(email || "\u2014")}</div>
                    <div class="em-account-plan">
                        <span class="em-badge ${statusClass}">${statusBadge}</span>
                        ${sub?.plan ? `<span class="em-account-plan-name">${escapeHtml(sub.plan)} plan</span>` : ""}
                    </div>
                </div>
            </div>
            ${renewalText ? `<div class="em-account-renewal">${escapeHtml(renewalText)}</div>` : ""}
            <div class="em-account-actions">${actionHtml}</div>
        </div>
    `;

    // Bind events
    const subscribeBtn = document.getElementById("subscribeBtn");
    if (subscribeBtn) {
        subscribeBtn.addEventListener("click", async () => {
            subscribeBtn.disabled = true;
            subscribeBtn.textContent = "Redirecting...";
            try {
                const result = await startCheckout();
                window.location.href = result.url;
            } catch (err) {
                const hint = document.getElementById("subscribeHint");
                if (hint) hint.textContent = err.message || "Something went wrong.";
                subscribeBtn.disabled = false;
                subscribeBtn.textContent = sub?.status === "canceled" ? "Resubscribe" : "Subscribe Now";
            }
        });
    }

    const manageBtn = document.getElementById("manageBtn");
    if (manageBtn) {
        manageBtn.addEventListener("click", async () => {
            manageBtn.disabled = true;
            manageBtn.textContent = "Redirecting...";
            try {
                const result = await openPortal();
                window.location.href = result.url;
            } catch (err) {
                showError(`Failed to open billing portal: ${err.message}`);
                manageBtn.disabled = false;
                manageBtn.textContent = "Manage Billing";
            }
        });
    }
}
