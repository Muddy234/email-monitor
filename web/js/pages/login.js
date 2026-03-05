/**
 * Login page logic — login/signup toggle, auth calls, redirect on success.
 */
import { supabase } from "../supabase-client.js";
import { signIn, signUp } from "../auth.js";

// If already logged in, redirect to dashboard
const { data: { session } } = await supabase.auth.getSession();
if (session) {
    window.location.replace("/app/dashboard.html");
}

const emailInput = document.getElementById("authEmail");
const passwordInput = document.getElementById("authPassword");
const loginBtn = document.getElementById("loginBtn");
const toggleBtn = document.getElementById("toggleAuth");
const errorEl = document.getElementById("authError");

let isSignUp = false;

function showErr(msg) {
    errorEl.textContent = msg;
    errorEl.classList.add("visible");
}

function hideErr() {
    errorEl.classList.remove("visible");
}

loginBtn.addEventListener("click", async () => {
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
            const result = await signUp(email, password);
            if (result.session) {
                window.location.replace("/app/dashboard.html");
            } else {
                loginBtn.disabled = false;
                loginBtn.textContent = "Sign Up";
                showErr("Check your email to confirm your account");
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

toggleBtn.addEventListener("click", () => {
    isSignUp = !isSignUp;
    loginBtn.textContent = isSignUp ? "Sign Up" : "Log In";
    toggleBtn.textContent = isSignUp
        ? "Already have an account? Log in"
        : "Don't have an account? Sign up";
    hideErr();
});

passwordInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") loginBtn.click();
});
