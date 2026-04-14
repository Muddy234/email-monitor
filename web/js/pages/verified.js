/**
 * Verified page — handles the email confirmation redirect from Supabase.
 *
 * Supabase appends auth tokens to the URL hash after confirming the email.
 * The SDK auto-detects them on init and writes the session to localStorage.
 * web-auth-sync.js (content script) then syncs it to the extension.
 *
 * When the OTP is consumed by a corporate email scanner (e.g. Safe Links),
 * the hash will contain an error but the email IS confirmed. Show a
 * success message in both cases — the extension popup auto-logs in with
 * stored credentials.
 */
import { supabase } from "../supabase-client.js";

const msgEl = document.getElementById("verifiedMessage");
const errorEl = document.getElementById("verifiedError");

// Check for OTP-expired error in hash (email still confirmed by scanner)
const hashParams = new URLSearchParams(location.hash.replace("#", ""));
const isOtpExpired = hashParams.get("error_code") === "otp_expired";

// Supabase SDK processes the URL hash tokens during createClient().
// By the time we call getSession(), the session should be established.
const { data: { session }, error } = await supabase.auth.getSession();

if (session || isOtpExpired) {
    // Session established, or email was confirmed but OTP consumed by scanner
    msgEl.style.display = "block";
} else {
    errorEl.style.display = "block";
}
