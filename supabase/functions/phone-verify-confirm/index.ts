import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const E164_REGEX = /^\+[1-9]\d{1,14}$/;
const CODE_REGEX = /^\d{4,10}$/;

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

    const { userId, phone, code } = await req.json();

    if (!userId || !phone || !code) {
      return new Response(
        JSON.stringify({ error: "userId, phone, and code are required" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (!E164_REGEX.test(phone)) {
      return new Response(
        JSON.stringify({ error: "Phone must be in E.164 format (e.g. +15551234567)" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (!CODE_REGEX.test(code)) {
      return new Response(
        JSON.stringify({ error: "Code must be a numeric verification code" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Check code via Twilio Verify API
    const twilioAuth = btoa(`${twilioAccountSid}:${twilioAuthToken}`);
    const checkRes = await fetch(
      `https://verify.twilio.com/v2/Services/${verifyServiceSid}/VerificationCheck`,
      {
        method: "POST",
        headers: {
          Authorization: `Basic ${twilioAuth}`,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({
          To: phone,
          Code: code,
        }),
      },
    );

    const checkData = await checkRes.json();

    if (!checkRes.ok || checkData.status !== "approved") {
      return new Response(
        JSON.stringify({ error: "Invalid verification code" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Code verified — now confirm the user's email in Supabase
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

    // Mark email as confirmed (phone is the alternative proof of identity)
    const { error: confirmError } = await supabase.auth.admin.updateUserById(userId, {
      email_confirm: true,
    });

    if (confirmError) {
      console.error("Failed to confirm user email:", confirmError);
      return new Response(
        JSON.stringify({ error: "Verification succeeded but account confirmation failed" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Generate a session for the now-confirmed user
    const { data: sessionData, error: sessionError } =
      await supabase.auth.admin.generateLink({
        type: "magiclink",
        email: userData.user.email!,
      });

    if (sessionError || !sessionData) {
      console.error("Failed to generate login link:", sessionError);
      return new Response(
        JSON.stringify({ confirmed: true, message: "Account confirmed. Please log in with your email and password." }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Extract the token from the magic link and exchange for a session
    const linkUrl = new URL(sessionData.properties.action_link);
    const tokenHash = linkUrl.searchParams.get("token") ||
      linkUrl.hash.replace("#", "").split("&").find((p: string) => p.startsWith("token="))?.split("=")[1];

    const verifyRes = await fetch(`${supabaseUrl}/auth/v1/verify`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        apikey: serviceRoleKey,
      },
      body: JSON.stringify({
        type: "magiclink",
        token_hash: tokenHash,
      }),
    });

    if (verifyRes.ok) {
      const session = await verifyRes.json();
      return new Response(
        JSON.stringify({
          access_token: session.access_token,
          refresh_token: session.refresh_token,
          user: session.user,
        }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Fallback: tell client to log in with their credentials
    return new Response(
      JSON.stringify({
        confirmed: true,
        message: "Account confirmed. Please log in with your email and password.",
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (err) {
    console.error("phone-verify-confirm error:", err);
    return new Response(
      JSON.stringify({ error: err.message || "Internal error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
