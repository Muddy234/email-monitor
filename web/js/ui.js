/**
 * Shared UI helpers — error banner, empty states, URL state, error logging.
 */
import { supabase } from "./supabase-client.js";

// -------------------------------------------------------------------------
// Error banner
// -------------------------------------------------------------------------

/**
 * Show error banner at top of .em-main.
 * Creates the banner DOM if it doesn't exist yet.
 */
export function showError(msg) {
    let banner = document.getElementById("em-error-banner");
    if (!banner) {
        banner = document.createElement("div");
        banner.id = "em-error-banner";
        banner.className = "em-error-banner";
        banner.innerHTML = `
            <span id="em-error-text"></span>
            <button class="em-error-dismiss" onclick="this.parentElement.classList.remove('visible')">&times;</button>
        `;
        const main = document.querySelector(".em-main");
        if (main) main.prepend(banner);
    }
    document.getElementById("em-error-text").textContent = msg;
    banner.classList.add("visible");
}

export function hideError() {
    const banner = document.getElementById("em-error-banner");
    if (banner) banner.classList.remove("visible");
}

// -------------------------------------------------------------------------
// Empty states
// -------------------------------------------------------------------------

/**
 * Render an empty state message inside a container.
 * @param {HTMLElement} container
 * @param {string} message
 */
export function showEmpty(container, message) {
    container.innerHTML = `
        <div class="em-empty">
            <div class="em-empty-icon">&#9993;</div>
            <div>${message}</div>
        </div>
    `;
}

// -------------------------------------------------------------------------
// URL state helpers
// -------------------------------------------------------------------------

/**
 * Get a URL search param value.
 * @param {string} key
 * @param {string} [fallback]
 * @returns {string}
 */
export function getParam(key, fallback = "") {
    const params = new URLSearchParams(window.location.search);
    return params.get(key) || fallback;
}

/**
 * Set a URL search param without page reload.
 * @param {string} key
 * @param {string} value — pass empty string to remove the param.
 */
export function setParam(key, value) {
    const params = new URLSearchParams(window.location.search);
    if (value) {
        params.set(key, value);
    } else {
        params.delete(key);
    }
    const qs = params.toString();
    const url = window.location.pathname + (qs ? `?${qs}` : "");
    history.replaceState(null, "", url);
}

// -------------------------------------------------------------------------
// Global error logging → Supabase error_logs table
// -------------------------------------------------------------------------

async function logErrorToSupabase(errorMsg, stack) {
    try {
        const { data: { session } } = await supabase.auth.getSession();
        if (!session) return;
        await supabase.from("error_logs").insert({
            user_id: session.user.id,
            page: window.location.pathname,
            error: (errorMsg || "").substring(0, 500),
            stack: (stack || "").substring(0, 2000),
        });
    } catch (_) {
        // Don't throw from error handler
    }
}

window.onerror = (msg, source, line, col, err) => {
    logErrorToSupabase(String(msg), err?.stack || `${source}:${line}:${col}`);
};

window.onunhandledrejection = (event) => {
    const err = event.reason;
    logErrorToSupabase(
        err?.message || String(err),
        err?.stack || ""
    );
};

// -------------------------------------------------------------------------
// Misc helpers
// -------------------------------------------------------------------------

/**
 * Format a date string to a short locale format.
 */
export function formatDate(dateStr) {
    if (!dateStr) return "\u2014";
    const d = new Date(dateStr);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/**
 * Format a duration in ms to a human-readable string.
 */
export function formatDuration(ms) {
    if (!ms || ms < 0) return "\u2014";
    if (ms < 1000) return `${ms}ms`;
    const secs = Math.round(ms / 1000);
    if (secs < 60) return `${secs}s`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

/**
 * Relative time from a timestamp.
 */
export function relativeTime(dateStr) {
    if (!dateStr) return "\u2014";
    const diff = Date.now() - new Date(dateStr).getTime();
    if (diff < 60_000) return "just now";
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    return `${Math.floor(diff / 86_400_000)}d ago`;
}

// -------------------------------------------------------------------------
// Toast notifications
// -------------------------------------------------------------------------

/**
 * Show a non-blocking toast notification (bottom-right).
 * @param {string} message
 * @param {"success"|"error"} type
 */
/**
 * Escape a string for safe insertion into innerHTML.
 */
export function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

export function showToast(message, type = "success") {
    let container = document.querySelector(".em-toast-container");
    if (!container) {
        container = document.createElement("div");
        container.className = "em-toast-container";
        document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = `em-toast em-toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 3400);
}
