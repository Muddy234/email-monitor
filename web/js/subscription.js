/**
 * Subscription module — fetch status, check active, show paywall/billing.
 */
import { supabase } from "./supabase-client.js";

const SUPABASE_FUNCTIONS_URL = "https://frbvdoszenrrlswegsxq.supabase.co/functions/v1";

// -------------------------------------------------------------------------
// Fetch subscription
// -------------------------------------------------------------------------

/**
 * Get the current user's subscription row.
 * @returns {Promise<object|null>} { status, plan, current_period_end, ... }
 */
export async function getSubscription() {
    const { data: { session } } = await supabase.auth.getSession();
    if (!session) return null;

    const { data, error } = await supabase
        .from("subscriptions")
        .select("status, plan, current_period_start, current_period_end, cancel_at_period_end, stripe_customer_id")
        .eq("user_id", session.user.id)
        .single();

    if (error) {
        console.error("Failed to fetch subscription:", error.message);
        return null;
    }
    return data;
}

/**
 * Check if a subscription grants access (active, trialing, or past_due grace).
 */
export function isSubscriptionActive(sub) {
    return sub?.status === "active" || sub?.status === "trialing" || sub?.status === "past_due";
}

/**
 * Check if this is a grandfathered user (active, no period end, no Stripe ID).
 */
export function isGrandfathered(sub) {
    return sub?.status === "active" && !sub?.current_period_end && !sub?.stripe_customer_id;
}

// -------------------------------------------------------------------------
// Checkout + portal redirects
// -------------------------------------------------------------------------

async function callEdgeFunction(name) {
    const { data: { session } } = await supabase.auth.getSession();
    if (!session) throw new Error("Not authenticated");

    const resp = await fetch(`${SUPABASE_FUNCTIONS_URL}/${name}`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${session.access_token}`,
            apikey: session.access_token,
        },
    });

    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `Edge function error: ${resp.status}`);
    }
    return resp.json();
}

/**
 * Start checkout flow. Returns { url, type: "checkout" | "portal" }.
 */
export async function startCheckout() {
    return callEdgeFunction("create-checkout-session");
}

/**
 * Open Stripe billing portal. Returns { url }.
 */
export async function openPortal() {
    return callEdgeFunction("create-portal-session");
}

// -------------------------------------------------------------------------
// Checkout success polling
// -------------------------------------------------------------------------

/**
 * Poll for subscription activation after checkout redirect.
 * Resolves with the subscription when active, or null after timeout.
 */
export function pollForActivation(maxAttempts = 5, intervalMs = 2000) {
    return new Promise((resolve) => {
        let attempts = 0;
        const timer = setInterval(async () => {
            attempts++;
            const sub = await getSubscription();
            if (sub?.status === "active") {
                clearInterval(timer);
                resolve(sub);
            } else if (attempts >= maxAttempts) {
                clearInterval(timer);
                resolve(null);
            }
        }, intervalMs);
    });
}

// -------------------------------------------------------------------------
// Paywall overlay
// -------------------------------------------------------------------------

/**
 * Inject the paywall overlay into the page. Blocks interaction until subscribed.
 * @param {string} status - The subscription status (inactive, canceled, etc.)
 */
export function showPaywall(status) {
    // Remove any existing paywall
    document.getElementById("em-paywall")?.remove();

    const overlay = document.createElement("div");
    overlay.id = "em-paywall";
    overlay.innerHTML = `
        <div class="em-paywall-backdrop">
            <div class="em-paywall-card">
                <h2>Subscribe to Clarion AI</h2>
                <p>Your inbox, intelligently managed. AI-powered email classification and draft generation.</p>
                <div class="em-paywall-price">$10 <span>/ month</span></div>
                <button class="em-btn em-btn-primary em-paywall-cta" id="paywallSubscribeBtn">
                    ${status === "canceled" ? "Resubscribe" : "Subscribe Now"}
                </button>
                <p class="em-paywall-hint" id="paywallHint"></p>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    document.getElementById("paywallSubscribeBtn").addEventListener("click", async () => {
        const btn = document.getElementById("paywallSubscribeBtn");
        btn.disabled = true;
        btn.textContent = "Redirecting...";
        try {
            const result = await startCheckout();
            window.location.href = result.url;
        } catch (err) {
            document.getElementById("paywallHint").textContent = err.message || "Something went wrong.";
            btn.disabled = false;
            btn.textContent = status === "canceled" ? "Resubscribe" : "Subscribe Now";
        }
    });
}

/**
 * Show a non-blocking warning banner for past_due users.
 */
export function showPastDueBanner() {
    if (document.getElementById("em-past-due-banner")) return;

    const banner = document.createElement("div");
    banner.id = "em-past-due-banner";
    banner.className = "em-past-due-banner";
    banner.innerHTML = `
        <span>Your payment failed. Please update your payment method to avoid interruption.</span>
        <button class="em-btn em-btn-sm" id="pastDueUpdateBtn">Update Payment</button>
    `;

    // Insert before main content
    const main = document.querySelector(".em-main");
    if (main) main.prepend(banner);

    document.getElementById("pastDueUpdateBtn").addEventListener("click", async () => {
        const btn = document.getElementById("pastDueUpdateBtn");
        btn.disabled = true;
        btn.textContent = "Redirecting...";
        try {
            const result = await openPortal();
            window.location.href = result.url;
        } catch (err) {
            btn.disabled = false;
            btn.textContent = "Update Payment";
        }
    });
}

/**
 * Show "Confirming your payment..." spinner during checkout polling.
 */
export function showCheckoutSpinner() {
    document.getElementById("em-paywall")?.remove();

    const overlay = document.createElement("div");
    overlay.id = "em-paywall";
    overlay.innerHTML = `
        <div class="em-paywall-backdrop">
            <div class="em-paywall-card">
                <div class="em-spinner"></div>
                <h2>Confirming your payment...</h2>
                <p class="em-paywall-hint" id="checkoutSpinnerHint">This should only take a moment.</p>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);
}

/**
 * Full subscription gate. Call after requireAuth() on every protected page.
 * Handles: checkout success polling, paywall, past-due banner.
 * @returns {Promise<object|null>} The subscription if active, or null (paywall shown).
 */
export async function requireSubscription() {
    // Handle checkout success redirect — poll for activation
    const params = new URLSearchParams(window.location.search);
    if (params.get("checkout") === "success") {
        // Strip param to prevent re-trigger on refresh
        params.delete("checkout");
        const cleanUrl = `${window.location.pathname}${params.toString() ? "?" + params : ""}`;
        history.replaceState(null, "", cleanUrl);

        showCheckoutSpinner();
        const sub = await pollForActivation(5, 2000);
        if (sub) {
            document.getElementById("em-paywall")?.remove();
            return sub;
        }
        // Timeout — show fallback message
        const hint = document.getElementById("checkoutSpinnerHint");
        if (hint) {
            hint.textContent = "Payment received. Your account is being activated — please refresh in a moment.";
        }
        return null;
    }

    const sub = await getSubscription();

    // No subscription row at all (shouldn't happen, but handle it)
    if (!sub) {
        showPaywall("inactive");
        return null;
    }

    if (isSubscriptionActive(sub)) {
        // Show past-due warning banner (non-blocking)
        if (sub.status === "past_due") {
            showPastDueBanner();
        }
        return sub;
    }

    // Inactive or canceled — show paywall
    showPaywall(sub.status);
    return null;
}
