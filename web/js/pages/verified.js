/**
 * Verified page — handles the email confirmation redirect from Supabase.
 *
 * Supabase appends auth tokens to the URL hash after confirming the email.
 * The SDK auto-detects them on init and writes the session to localStorage.
 * web-auth-sync.js (content script) then syncs it to the extension.
 */
import { supabase } from "../supabase-client.js";

const msgEl = document.getElementById("verifiedMessage");
const errorEl = document.getElementById("verifiedError");

// Supabase SDK processes the URL hash tokens during createClient().
// By the time we call getSession(), the session should be established.
const { data: { session }, error } = await supabase.auth.getSession();

if (session) {
    msgEl.style.display = "block";
} else {
    errorEl.style.display = "block";
}
