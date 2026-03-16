import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const E164_REGEX = /^\+[1-9]\d{1,14}$/;
const CODE_REGEX = /^\d{6}$/;
const MAX_ATTEMPTS = 5;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

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
        JSON.stringify({ error: "Code must be a 6-digit number" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Look up the verification record
    const { data: verifyRow, error: lookupError } = await supabase
      .from("phone_verifications")
      .select("*")
      .eq("user_id", userId)
      .eq("phone", phone)
      .gt("expires_at", new Date().toISOString())
      .order("created_at", { ascending: false })
      .limit(1)
      .single();

    if (lookupError || !verifyRow) {
      return new Response(
        JSON.stringify({ error: "No valid verification found. Please request a new code." }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    if (verifyRow.attempts >= MAX_ATTEMPTS) {
      await supabase.from("phone_verifications").delete().eq("id", verifyRow.id);
      return new Response(
        JSON.stringify({ error: "Too many attempts. Please request a new code." }),
        { status: 429, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Hash the submitted code and compare
    const encoder = new TextEncoder();
    const hashBuffer = await crypto.subtle.digest("SHA-256", encoder.encode(code));
    const submittedHash = Array.from(new Uint8Array(hashBuffer))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");

    if (submittedHash !== verifyRow.code_hash) {
      // Increment attempts
      await supabase
        .from("phone_verifications")
        .update({ attempts: verifyRow.attempts + 1 })
        .eq("id", verifyRow.id);

      return new Response(
        JSON.stringify({ error: "Invalid verification code" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Code is correct — clean up verification record
    await supabase.from("phone_verifications").delete().eq("id", verifyRow.id);

    // Verify user exists and is still unconfirmed
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
        JSON.stringify({ error: "Account confirmed but session creation failed. Please log in manually." }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Extract the token from the magic link and use it to get a session
    const linkUrl = new URL(sessionData.properties.action_link);
    const tokenHash = linkUrl.searchParams.get("token") ||
      linkUrl.hash.replace("#", "").split("&").find((p: string) => p.startsWith("token="))?.split("=")[1];

    // Use the OTP verification endpoint to exchange the magic link token for a session
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

    // If verify endpoint didn't work, try direct token exchange via GoTrue
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
