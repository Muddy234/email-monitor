/**
 * Verified page — handles the email confirmation redirect from Supabase.
 *
 * Supabase appends auth tokens to the URL hash after confirming the email.
 * The SDK auto-detects them on init and writes the session to localStorage.
 * web-auth-sync.js (content script) then syncs it to the extension.
 *
 * Corporate email scanners (Safe Links, Mimecast, etc.) often pre-fetch
 * the link which consumes the single-use OTP. When that happens the URL
 * hash contains error_code=otp_expired but the email is still confirmed
 * server-side. The extension popup will auto-login with stored
 * credentials, so we show the success message in that case too.
 */
import { supabase } from "../supabase-client.js";

const msgEl = document.getElementById("verifiedMessage");
const errorEl = document.getElementById("verifiedError");

const hashParams = new URLSearchParams(location.hash.replace("#", ""));
const isOtpExpired = hashParams.get("error_code") === "otp_expired";

// Supabase SDK processes the URL hash tokens during createClient().
// By the time we call getSession(), the session should be established.
const { data: { session }, error } = await supabase.auth.getSession();

if (session || isOtpExpired) {
    msgEl.style.display = "block";
} else {
    errorEl.style.display = "block";
}
