import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const E164_REGEX = /^\+[1-9]\d{1,14}$/;
const CODE_REGEX = /^\d{6}$/;

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

    // --- Validation ---
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

    // Verify the OTP code via Supabase's built-in phone auth
    const { data: verifyData, error: verifyError } = await supabase.auth.verifyOtp({
      phone,
      token: code,
      type: "sms",
    });

    if (verifyError) {
      console.error("OTP verification failed:", verifyError);
      return new Response(
        JSON.stringify({ error: verifyError.message || "Invalid or expired verification code" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Phone OTP verified successfully.
    // Side-door: mark the user's email as confirmed and clear the phone number.
    // This is intentional — phone verification serves as an alternative identity
    // proof for users whose email confirmation was blocked by corporate gateways
    // (e.g. Mimecast, Proofpoint). The phone number is cleared after verification
    // to minimize stored PII since it's only used for this one-time verification,
    // not for ongoing notifications or account recovery.
    const { error: confirmError } = await supabase.auth.admin.updateUserById(userId, {
      email_confirm: true,
      phone: "",
    });

    if (confirmError) {
      console.error("Failed to confirm user email:", confirmError);
      return new Response(
        JSON.stringify({ error: "Verification succeeded but account confirmation failed" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Return the session from verifyOtp so the client can log in
    return new Response(
      JSON.stringify({
        access_token: verifyData.session?.access_token,
        refresh_token: verifyData.session?.refresh_token,
        user: verifyData.user,
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
