import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const stripeKey = Deno.env.get("STRIPE_SECRET_KEY");
    const priceId = Deno.env.get("STRIPE_PRICE_ID");
    if (!stripeKey || !priceId) {
      return new Response(
        JSON.stringify({ error: "Stripe not configured" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Verify JWT and extract user
    const authHeader = req.headers.get("Authorization");
    if (!authHeader) {
      return new Response(
        JSON.stringify({ error: "Not authenticated" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseAnonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
    const supabase = createClient(supabaseUrl, supabaseAnonKey, {
      global: { headers: { Authorization: authHeader } },
    });

    const { data: { user }, error: authError } = await supabase.auth.getUser();
    if (authError || !user) {
      return new Response(
        JSON.stringify({ error: "Invalid session" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Check if user already has an active subscription (prevent duplicates).
    // If active, redirect to portal instead of creating a new checkout.
    // NOTE: This means an active user who somehow hits "Subscribe" (double-click,
    // bookmark, back button) lands in the portal (billing management) instead of
    // a checkout page. That's correct — they need to manage their existing
    // subscription, not create a second one.
    const { data: sub } = await supabase
      .from("subscriptions")
      .select("status, stripe_customer_id")
      .eq("user_id", user.id)
      .single();

    if (sub?.status === "active" && sub?.stripe_customer_id) {
      // Create a portal session instead
      const portalResp = await fetch(
        "https://api.stripe.com/v1/billing_portal/sessions",
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${stripeKey}`,
            "Content-Type": "application/x-www-form-urlencoded",
          },
          body: new URLSearchParams({
            customer: sub.stripe_customer_id,
            return_url: `${req.headers.get("origin") || "https://clarion.ai"}/app/dashboard`,
          }),
        },
      );
      const portal = await portalResp.json();
      if (portal.url) {
        return new Response(
          JSON.stringify({ url: portal.url, type: "portal" }),
          { headers: { ...corsHeaders, "Content-Type": "application/json" } },
        );
      }
    }

    // Build Stripe Checkout Session params
    const params: Record<string, string> = {
      "mode": "subscription",
      "line_items[0][price]": priceId,
      "line_items[0][quantity]": "1",
      "client_reference_id": user.id,
      "success_url": `${req.headers.get("origin") || "https://clarion.ai"}/app/dashboard?checkout=success`,
      "cancel_url": `${req.headers.get("origin") || "https://clarion.ai"}/app/dashboard?checkout=canceled`,
    };

    // Reuse existing Stripe customer if available, otherwise pre-fill email
    if (sub?.stripe_customer_id) {
      params["customer"] = sub.stripe_customer_id;
    } else {
      params["customer_email"] = user.email || "";
    }

    const checkoutResp = await fetch(
      "https://api.stripe.com/v1/checkout/sessions",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${stripeKey}`,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams(params),
      },
    );

    const checkout = await checkoutResp.json();
    if (!checkout.url) {
      console.error("Stripe checkout creation failed:", checkout);
      return new Response(
        JSON.stringify({ error: "Failed to create checkout session" }),
        { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    return new Response(
      JSON.stringify({ url: checkout.url, type: "checkout" }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("create-checkout-session error:", err);
    return new Response(
      JSON.stringify({ error: err.message || "Internal error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
