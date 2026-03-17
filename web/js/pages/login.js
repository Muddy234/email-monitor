/**
 * Login page logic — login/signup toggle, auth calls, redirect on success.
 * Includes phone OTP fallback for users whose email verification is blocked
 * by corporate gateways (Mimecast, Proofpoint, etc.).
 */
import { supabase } from "../supabase-client.js";
import { signIn, signUp } from "../auth.js";

const SUPABASE_URL = "https://frbvdoszenrrlswegsxq.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZyYnZkb3N6ZW5ycmxzd2Vnc3hxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI2NjA0OTUsImV4cCI6MjA4ODIzNjQ5NX0.OCYTv_B823u_9o_Q9S-qPpUea9DQt_xpsWuNnolJT7M";

// If already logged in, redirect to dashboard
const { data: { session } } = await supabase.auth.getSession();
if (session) {
    window.location.replace("/app/dashboard.html");
}

const nameInput = document.getElementById("authName");
const nameGroup = document.getElementById("nameGroup");
const emailInput = document.getElementById("authEmail");
const passwordInput = document.getElementById("authPassword");
const loginBtn = document.getElementById("loginBtn");
const toggleBtn = document.getElementById("toggleAuth");
const errorEl = document.getElementById("authError");

// Phone verify elements
const phoneVerifySection = document.getElementById("phoneVerifySection");
const phoneVerifyToggle = document.getElementById("phoneVerifyToggle");
const phoneInputGroup = document.getElementById("phoneInputGroup");
const phoneInput = document.getElementById("phoneInput");
const sendCodeBtn = document.getElementById("sendCodeBtn");
const codeInputGroup = document.getElementById("codeInputGroup");
const otpInput = document.getElementById("otpInput");
const verifyCodeBtn = document.getElementById("verifyCodeBtn");

let isSignUp = false;
let pendingUserId = null;
let pendingPhone = null;

function showErr(msg) {
    errorEl.textContent = msg;
    errorEl.classList.add("visible");
}

function hideErr() {
    errorEl.classList.remove("visible");
}

/**
 * Format a raw phone string to E.164.
 * Strips non-digits, prepends +1 for 10-digit US numbers.
 * NOTE: Hardcodes US country code. Add country selector for international users later.
 */
function formatPhoneE164(raw) {
    const digits = raw.replace(/\D/g, "");
    if (digits.length === 10) return `+1${digits}`;
    if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
    if (raw.startsWith("+")) return raw.replace(/[^\d+]/g, "");
    return `+${digits}`;
}

/** Show the phone verify section and hide the signup form fields. */
function showPhoneVerifyFlow() {
    phoneVerifySection.style.display = "";
    phoneVerifyToggle.style.display = "";
    phoneInputGroup.style.display = "none";
    codeInputGroup.style.display = "none";
    nameGroup.style.display = "none";
    loginBtn.style.display = "none";
    toggleBtn.style.display = "none";
    document.querySelector(".em-form-group:has(#authEmail)")?.style.setProperty("display", "none");
    document.querySelector(".em-form-group:has(#authPassword)")?.style.setProperty("display", "none");
    hideErr();
}

async function callEdgeFunction(name, body) {
    const res = await fetch(`${SUPABASE_URL}/functions/v1/${name}`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            apikey: SUPABASE_ANON_KEY,
        },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
}

document.getElementById("authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideErr();
    const email = emailInput.value.trim();
    const password = passwordInput.value;

    if (!email || !password) {
        showErr("Email and password are required");
        return;
    }

    loginBtn.disabled = true;
    loginBtn.textContent = isSignUp ? "Signing up..." : "Logging in...";

    try {
        if (isSignUp) {
            const displayName = nameInput.value.trim();
            if (!displayName) {
                showErr("Please enter the name you'd like used in email sign-offs");
                loginBtn.disabled = false;
                loginBtn.textContent = "Sign Up";
                return;
            }
            const result = await signUp(email, password, displayName);
            if (result.session) {
                window.location.replace("/app/dashboard.html");
            } else {
                // No session = email confirmation pending.
                // Store userId for phone verify fallback.
                // If user.identities is empty, this is a re-signup for an existing
                // unconfirmed account — Supabase re-sends the confirmation email.
                pendingUserId = result.user?.id || null;
                loginBtn.disabled = false;
                loginBtn.textContent = "Sign Up";
                showPhoneVerifyFlow();
            }
        } else {
            await signIn(email, password);
            window.location.replace("/app/dashboard.html");
        }
    } catch (err) {
        showErr(err.message || "Authentication failed");
        loginBtn.disabled = false;
        loginBtn.textContent = isSignUp ? "Sign Up" : "Log In";
    }
});

// "Verify by phone" toggle
phoneVerifyToggle.addEventListener("click", () => {
    phoneVerifyToggle.style.display = "none";
    phoneInputGroup.style.display = "";
    document.getElementById("verifyHint").textContent = "Enter your phone number to verify your account";
});

// Send code
sendCodeBtn.addEventListener("click", async () => {
    hideErr();
    const raw = phoneInput.value.trim();
    if (!raw) {
        showErr("Please enter your phone number");
        return;
    }

    if (!pendingUserId) {
        showErr("No pending signup found. Please sign up again.");
        return;
    }

    const phone = formatPhoneE164(raw);
    sendCodeBtn.disabled = true;
    sendCodeBtn.textContent = "Sending...";

    try {
        await callEdgeFunction("phone-verify-start", { userId: pendingUserId, phone });
        pendingPhone = phone;
        phoneInputGroup.style.display = "none";
        codeInputGroup.style.display = "";
        document.getElementById("verifyHint").textContent = "Enter the code sent to your phone";
    } catch (err) {
        showErr(err.message || "Failed to send verification code");
    } finally {
        sendCodeBtn.disabled = false;
        sendCodeBtn.textContent = "Send Code";
    }
});

// Verify code
verifyCodeBtn.addEventListener("click", async () => {
    hideErr();
    const code = otpInput.value.trim();
    if (!code) {
        showErr("Please enter the verification code");
        return;
    }

    if (!pendingUserId || !pendingPhone) {
        showErr("No pending verification. Please start over.");
        return;
    }

    verifyCodeBtn.disabled = true;
    verifyCodeBtn.textContent = "Verifying...";

    try {
        const data = await callEdgeFunction("phone-verify-confirm", {
            userId: pendingUserId,
            phone: pendingPhone,
            code,
        });

        if (data.access_token) {
            // Set the session and redirect
            await supabase.auth.setSession({
                access_token: data.access_token,
                refresh_token: data.refresh_token,
            });
            window.location.replace("/app/dashboard.html");
        } else if (data.confirmed) {
            // Account confirmed but no auto-session — sign in with stored credentials
            const email = emailInput.value.trim();
            const password = passwordInput.value;
            if (email && password) {
                await signIn(email, password);
                window.location.replace("/app/dashboard.html");
            } else {
                window.location.replace("/app/login.html");
            }
        } else {
            showErr("Verification succeeded but no session returned. Please try logging in.");
        }
    } catch (err) {
        showErr(err.message || "Verification failed");
        verifyCodeBtn.disabled = false;
        verifyCodeBtn.textContent = "Verify";
    }
});

toggleBtn.addEventListener("click", () => {
    isSignUp = !isSignUp;
    loginBtn.textContent = isSignUp ? "Sign Up" : "Log In";
    toggleBtn.textContent = isSignUp
        ? "Already have an account? Log in"
        : "Don't have an account? Sign up";
    nameGroup.style.display = isSignUp ? "" : "none";

    // Reset phone verify state when toggling
    phoneVerifySection.style.display = "none";
    phoneInputGroup.style.display = "none";
    codeInputGroup.style.display = "none";
    loginBtn.style.display = "";
    document.querySelector(".em-form-group:has(#authEmail)")?.style.setProperty("display", "");
    document.querySelector(".em-form-group:has(#authPassword)")?.style.setProperty("display", "");
    pendingUserId = null;
    pendingPhone = null;
    hideErr();
});

otpInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") verifyCodeBtn.click();
});
