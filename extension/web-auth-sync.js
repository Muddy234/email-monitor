/**
 * Content script — runs on clarion-ai.app pages.
 *
 * Bridges auth between the Chrome extension (chrome.storage.local)
 * and the Supabase JS SDK (localStorage) so login/logout on either
 * side propagates to the other.
 */

const WEB_STORAGE_KEY = "sb-frbvdoszenrrlswegsxq-auth-token";
const POLL_INTERVAL_MS = 5000;
const RELOAD_GUARD_KEY = "_clarion_auth_sync_reload";

// Track last-known web session user ID to detect changes
let lastWebUserId = null;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isValidSession(session) {
  return session && session.access_token && session.user?.id;
}

/** Read the Supabase SDK session from localStorage. */
function readWebSession() {
  try {
    const raw = localStorage.getItem(WEB_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return isValidSession(parsed) ? parsed : null;
  } catch (_) {
    return null;
  }
}

/** Read the extension session from chrome.storage.local. */
async function readExtSession() {
  const result = await chrome.storage.local.get("supabaseSession");
  const session = result.supabaseSession;
  return isValidSession(session) ? session : null;
}

/** Convert extension session → Supabase SDK localStorage format. */
function extToWeb(extSession) {
  return {
    access_token: extSession.access_token,
    refresh_token: extSession.refresh_token,
    expires_at: extSession.expires_at,
    expires_in: 3600,
    token_type: "bearer",
    user: extSession.user,
  };
}

/** Convert Supabase SDK localStorage session → extension format. */
function webToExt(webSession) {
  return {
    access_token: webSession.access_token,
    refresh_token: webSession.refresh_token,
    expires_at: webSession.expires_at,
    last_login_at: Math.floor(Date.now() / 1000),
    user: {
      id: webSession.user?.id,
      email: webSession.user?.email,
      name: webSession.user?.user_metadata?.full_name
        || webSession.user?.user_metadata?.name
        || webSession.user?.name
        || "",
    },
  };
}

/** Write session to localStorage for the Supabase SDK to pick up. */
function writeWebSession(extSession) {
  localStorage.setItem(WEB_STORAGE_KEY, JSON.stringify(extToWeb(extSession)));
}

/** Clear the web session from localStorage. */
function clearWebSession() {
  localStorage.removeItem(WEB_STORAGE_KEY);
}

/** Write session to chrome.storage.local for the extension popup. */
async function writeExtSession(webSession) {
  await chrome.storage.local.set({ supabaseSession: webToExt(webSession) });
  // Notify background so it can re-init Supabase (sync alarm, realtime)
  try {
    chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
  } catch (_) {}
}

/** Clear the extension session. */
async function clearExtSession() {
  await chrome.storage.local.remove("supabaseSession");
  try {
    chrome.runtime.sendMessage({ type: "supabaseSessionChanged" });
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Initial sync on page load
// ---------------------------------------------------------------------------

async function initialSync() {
  // 1. Read extension session first (async)
  const extSession = await readExtSession();

  // 2. Read web session AFTER async completes (prevents race with SDK init)
  const webSession = readWebSession();

  const extValid = isValidSession(extSession);
  const webValid = isValidSession(webSession);

  if (extValid && !webValid) {
    // Extension has session, web doesn't → push to web
    writeWebSession(extSession);
  } else if (webValid && !extValid) {
    // Web has session, extension doesn't → push to extension
    await writeExtSession(webSession);
  } else if (extValid && webValid) {
    // Both have sessions — check if same user
    if (extSession.user.id !== webSession.user.id) {
      // Different users → newest wins
      const extLoginAt = extSession.last_login_at || 0;
      const webLoginAt = Math.floor(Date.now() / 1000); // web session is "current" if user is on the page
      // If extension session has a recent last_login_at, compare
      if (extLoginAt > webLoginAt - 10) {
        // Extension login is very recent — it wins
        writeWebSession(extSession);
      } else {
        // Web session is current (user is on the page) — it wins
        await writeExtSession(webSession);
      }
    }
    // Same user → no action needed, each side refreshes its own tokens
  }
  // Neither valid → nothing to do

  // Initialize tracking
  lastWebUserId = readWebSession()?.user?.id || null;
}

// ---------------------------------------------------------------------------
// Poll for web-side auth changes (login/logout on the website)
// ---------------------------------------------------------------------------

function pollWebSession() {
  setInterval(async () => {
    const webSession = readWebSession();
    const currentWebUserId = webSession?.user?.id || null;

    if (currentWebUserId && currentWebUserId !== lastWebUserId) {
      // New web login detected → push to extension
      lastWebUserId = currentWebUserId;
      await writeExtSession(webSession);
    } else if (!currentWebUserId && lastWebUserId) {
      // Web logout detected → clear extension session
      lastWebUserId = null;
      await clearExtSession();
    }
  }, POLL_INTERVAL_MS);
}

// ---------------------------------------------------------------------------
// Listen for extension-side auth changes
// ---------------------------------------------------------------------------

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.supabaseSession) return;

  const newSession = changes.supabaseSession.newValue;

  if (isValidSession(newSession)) {
    // Extension session added/changed → push to localStorage
    writeWebSession(newSession);
    lastWebUserId = newSession.user.id;
  } else {
    // Extension session removed/invalidated → clear web session gracefully
    if (lastWebUserId) {
      clearWebSession();
      lastWebUserId = null;

      // Dispatch custom event instead of hard reload
      if (!sessionStorage.getItem(RELOAD_GUARD_KEY)) {
        sessionStorage.setItem(RELOAD_GUARD_KEY, "1");
        document.dispatchEvent(new CustomEvent("clarion:auth-revoked"));
        // Clear guard after a short delay so future logouts still work
        setTimeout(() => sessionStorage.removeItem(RELOAD_GUARD_KEY), 3000);
      }
    }
  }
});

// Listen for explicit re-sync messages from background
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "ext_session_changed") {
    // Re-read and sync immediately (don't wait for next poll)
    readExtSession().then((extSession) => {
      if (isValidSession(extSession)) {
        writeWebSession(extSession);
        lastWebUserId = extSession.user.id;
      }
    });
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

initialSync().then(() => {
  pollWebSession();
});
