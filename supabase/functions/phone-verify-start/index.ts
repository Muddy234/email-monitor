import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const E164_REGEX = /^\+[1-9]\d{1,14}$/;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const twilioAccountSid = Deno.env.get("TWILIO_ACCOUNT_SID")!;
    const twilioAuthToken = Deno.env.get("TWILIO_AUTH_TOKEN")!;
    const verifyServiceSid = Deno.env.get("TWILIO_VERIFY_SERVICE_SID")!;

    const supabase = createClient(supabaseUrl, serviceRoleKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

    // --- IP rate limit ---
    const clientIp =
      req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
      req.headers.get("cf-connecting-ip") ||
      "unknown";

    const { data: allowed, error: rlError } = await supabase.rpc(
      "check_phone_verify_rate_limit",
      { p_ip: clientIp, p_max_attempts: 10, p_window_minutes: 60 },
    );

    if (rlError) {
      console.error("Rate limit check failed:", rlError);
      // Fail open — don't block users if the check itself breaks
    } else if (allowed === false) {
      return new Response(
        JSON.stringify({ error: "Too many verification requests. Please try again later." }),
        { status: 429, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    const { userId, phone } = await req.json();

    if (!userId || !phone) {
      return new Response(
        JSON.stringify({ error: "userId and phone are required" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (!E164_REGEX.test(phone)) {
      return new Response(
        JSON.stringify({ error: "Phone must be in E.164 format (e.g. +15551234567)" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Verify user exists and is unconfirmed
    const { data: userData, error: userError } = await supabase.auth.admin.getUserById(userId);
    if (userError || !userData?.user) {
      return new Response(
        JSON.stringify({ error: "User not found" }),
        { status: 404, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (userData.user.email_confirmed_at) {
      return new Response(
        JSON.stringify({ error: "User is already confirmed" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Start verification via Twilio Verify API
    const twilioAuth = btoa(`${twilioAccountSid}:${twilioAuthToken}`);
    const verifyRes = await fetch(
      `https://verify.twilio.com/v2/Services/${verifyServiceSid}/Verifications`,
      {
        method: "POST",
        headers: {
          Authorization: `Basic ${twilioAuth}`,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({
          To: phone,
          Channel: "sms",
        }),
      },
    );

    if (!verifyRes.ok) {
      const verifyErr = await verifyRes.json();
      console.error("Twilio Verify failed:", verifyErr);
      return new Response(
        JSON.stringify({ error: verifyErr.message || "Failed to send verification code" }),
        { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    return new Response(
      JSON.stringify({ success: true }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("phone-verify-start error:", err);
    return new Response(
      JSON.stringify({ error: err.message || "Internal error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
