/**
 * Supabase Auth — email/password authentication via REST.
 * Loaded via importScripts() in the service worker.
 *
 * Depends on: supabase-config.js (SUPABASE_URL, SUPABASE_ANON_KEY)
 *
 * Session shape stored in chrome.storage.local:
 *   { access_token, refresh_token, expires_at, user }
 */

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function _authRequest(endpoint, body) {
  const resp = await fetch(`${SUPABASE_URL}/auth/v1${endpoint}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: SUPABASE_ANON_KEY,
    },
    body: JSON.stringify(body),
  });

  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error_description || data.msg || data.error || "Auth request failed");
  }
  return data;
}

function _sessionFromResponse(data) {
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600),
    user: {
      id: data.user?.id,
      email: data.user?.email,
    },
  };
}

async function _saveSession(session) {
  await chrome.storage.local.set({ supabaseSession: session });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

async function supabaseRefreshToken(refreshToken) {
  const data = await _authRequest("/token?grant_type=refresh_token", {
    refresh_token: refreshToken,
  });
  const session = _sessionFromResponse(data);
  await _saveSession(session);
  return session;
}

/**
 * Returns a valid access token, refreshing if within 5 min of expiry.
 * Returns null if no session exists.
 */
async function getValidAccessToken() {
  const result = await chrome.storage.local.get("supabaseSession");
  const session = result.supabaseSession;
  if (!session || !session.access_token) return null;

  const now = Math.floor(Date.now() / 1000);
  const buffer = 300; // 5 min

  if (session.expires_at - now > buffer) {
    return session.access_token;
  }

  // Needs refresh
  if (!session.refresh_token) return null;
  try {
    const refreshed = await supabaseRefreshToken(session.refresh_token);
    return refreshed.access_token;
  } catch (err) {
    if (DEBUG) console.warn("Supabase token refresh failed:", err.message);
    return null;
  }
}

/**
 * Get the current Supabase session from storage (no refresh).
 */
async function getSupabaseSession() {
  const result = await chrome.storage.local.get("supabaseSession");
  return result.supabaseSession || null;
}
