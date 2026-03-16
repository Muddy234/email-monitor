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
    const twilioMsgServiceSid = Deno.env.get("TWILIO_MESSAGING_SERVICE_SID")!;

    const supabase = createClient(supabaseUrl, serviceRoleKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

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

    // Rate limit: max 1 code per user per 60 seconds
    const { data: recentCodes } = await supabase
      .from("phone_verifications")
      .select("created_at")
      .eq("user_id", userId)
      .gte("created_at", new Date(Date.now() - 60_000).toISOString())
      .limit(1);

    if (recentCodes && recentCodes.length > 0) {
      return new Response(
        JSON.stringify({ error: "Please wait 60 seconds before requesting a new code" }),
        { status: 429, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Generate 6-digit OTP and hash it
    const code = String(Math.floor(100000 + Math.random() * 900000));
    const encoder = new TextEncoder();
    const hashBuffer = await crypto.subtle.digest("SHA-256", encoder.encode(code));
    const codeHash = Array.from(new Uint8Array(hashBuffer))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");

    // Delete any existing codes for this user, then insert new one
    await supabase.from("phone_verifications").delete().eq("user_id", userId);

    const { error: insertError } = await supabase.from("phone_verifications").insert({
      user_id: userId,
      phone,
      code_hash: codeHash,
      expires_at: new Date(Date.now() + 10 * 60_000).toISOString(), // 10 min expiry
    });

    if (insertError) {
      console.error("Failed to store verification code:", insertError);
      return new Response(
        JSON.stringify({ error: "Failed to create verification" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Send SMS via Twilio REST API
    const twilioUrl = `https://api.twilio.com/2010-04-01/Accounts/${twilioAccountSid}/Messages.json`;
    const twilioAuth = btoa(`${twilioAccountSid}:${twilioAuthToken}`);

    const smsRes = await fetch(twilioUrl, {
      method: "POST",
      headers: {
        Authorization: `Basic ${twilioAuth}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({
        To: phone,
        MessagingServiceSid: twilioMsgServiceSid,
        Body: `Your Clarion AI verification code is: ${code}`,
      }),
    });

    if (!smsRes.ok) {
      const smsErr = await smsRes.json();
      console.error("Twilio SMS failed:", smsErr);
      // Clean up the verification record
      await supabase.from("phone_verifications").delete().eq("user_id", userId);
      return new Response(
        JSON.stringify({ error: smsErr.message || "Failed to send SMS" }),
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
