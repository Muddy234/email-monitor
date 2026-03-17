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
        .select("status, plan, current_period_start, current_period_end, cancel_at_period_end, stripe_customer_id, trial_ends_at")
        .eq("user_id", session.user.id)
        .single();

    if (error) {
        console.error("Failed to fetch subscription:", error.message);
        return null;
    }
    return data;
}

/**
 * Check if a trial subscription has expired.
 */
export function isTrialExpired(sub) {
    if (sub?.status !== "trialing") return false;
    if (!sub?.trial_ends_at) return false;
    return new Date(sub.trial_ends_at) <= new Date();
}

/**
 * Get days remaining in trial, or null if not trialing.
 */
export function getTrialDaysRemaining(sub) {
    if (sub?.status !== "trialing" || !sub?.trial_ends_at) return null;
    const ms = new Date(sub.trial_ends_at) - Date.now();
    if (ms <= 0) return 0;
    return Math.ceil(ms / (1000 * 60 * 60 * 24));
}

/**
 * Check if a subscription grants access (active, trialing w/ time left, or past_due grace).
 */
export function isSubscriptionActive(sub) {
    if (sub?.status === "active" || sub?.status === "past_due") return true;
    if (sub?.status === "trialing") return !isTrialExpired(sub);
    return false;
}

/**
 * Gate access: redirects to account page if subscription is not active.
 * Returns the subscription object if access is granted.
 */
export async function ensureAccess() {
    const sub = await getSubscription();
    if (isSubscriptionActive(sub) || isGrandfathered(sub)) return sub;
    window.location.replace("/app/account.html");
    return new Promise(() => {});
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

