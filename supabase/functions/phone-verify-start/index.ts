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

    const supabase = createClient(supabaseUrl, serviceRoleKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

    const { userId, phone } = await req.json();

    // --- Validation ---
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

    // Check phone isn't already claimed by a different confirmed user
    const { data: existingUsers } = await supabase.auth.admin.listUsers();
    const phoneClaimed = existingUsers?.users?.some(
      (u) => u.phone === phone && u.id !== userId && u.email_confirmed_at,
    );
    if (phoneClaimed) {
      return new Response(
        JSON.stringify({ error: "This phone number is already associated with another account" }),
        { status: 409, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Store phone on user record so OTP can be sent
    const { error: updateError } = await supabase.auth.admin.updateUserById(userId, { phone });
    if (updateError) {
      console.error("Failed to update user phone:", updateError);
      return new Response(
        JSON.stringify({ error: "Failed to set phone number" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Send OTP via Supabase's built-in phone auth (uses configured Twilio provider)
    // Rate limiting (default 60s between sends) is enforced by Supabase.
    const { error: otpError } = await supabase.auth.signInWithOtp({ phone });
    if (otpError) {
      console.error("OTP send failed:", otpError);
      return new Response(
        JSON.stringify({ error: otpError.message || "Failed to send verification code" }),
        { status: 429, headers: { ...corsHeaders, "Content-Type": "application/json" } },
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
