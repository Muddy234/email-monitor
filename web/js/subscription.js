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

