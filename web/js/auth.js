/**
 * Auth module — login, signup, logout, session guard, expiry handling.
 */
import { supabase } from "./supabase-client.js";

/**
 * Blocking auth guard. Each protected page awaits this before rendering.
 * Redirects to login.html if no valid session.
 * @returns {Promise<object>} The current session.
 */
export async function requireAuth() {
    const { data: { session }, error } = await supabase.auth.getSession();
    if (error || !session) {
        window.location.replace("/app/login.html");
        // Hang so nothing else runs during redirect
        return new Promise(() => {});
    }
    return session;
}

/**
 * Listen for auth state changes (handles mid-session expiry).
 * Call once on page load after requireAuth resolves.
 */
export function listenAuthChanges() {
    supabase.auth.onAuthStateChange((event) => {
        if (event === "SIGNED_OUT" || event === "TOKEN_REFRESHED") {
            // TOKEN_REFRESHED is fine, SIGNED_OUT means redirect
            if (event === "SIGNED_OUT") {
                window.location.replace("/app/login.html");
            }
        }
    });
}

/**
 * Sign in with email/password.
 * @returns {Promise<object>} The session data.
 */
export async function signIn(email, password) {
    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;
    return data;
}

/**
 * Sign up with email/password.
 * @returns {Promise<object>} The signup response.
 */
export async function signUp(email, password) {
    const { data, error } = await supabase.auth.signUp({ email, password });
    if (error) throw error;
    return data;
}

/**
 * Sign out and redirect to login.
 */
export async function signOut() {
    await supabase.auth.signOut();
    window.location.replace("/app/login.html");
}

/**
 * Get the current user's email.
 * @returns {Promise<string|null>}
 */
export async function getUserEmail() {
    const { data: { session } } = await supabase.auth.getSession();
    return session?.user?.email || null;
}
