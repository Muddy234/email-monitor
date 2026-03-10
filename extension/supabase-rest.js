/**
 * Supabase PostgREST wrapper — push emails, read/update drafts.
 * Loaded via importScripts() in the service worker.
 *
 * Depends on: supabase-config.js, supabase-auth.js
 */

// ---------------------------------------------------------------------------
// Base request helper
// ---------------------------------------------------------------------------

async function supabaseRequest(path, options = {}) {
  const accessToken = await getValidAccessToken();
  if (!accessToken) throw new Error("Not authenticated with Supabase");

  const headers = {
    "Content-Type": "application/json",
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${accessToken}`,
    ...options.headers,
  };

  const resp = await fetch(`${SUPABASE_URL}/rest/v1${path}`, {
    method: options.method || "GET",
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.message || err.error || `Supabase ${resp.status}`);
  }

  // Some requests (204) return no body
  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

// ---------------------------------------------------------------------------
// Emails
// ---------------------------------------------------------------------------

/**
 * Upsert emails to the emails table.
 * Uses merge-duplicates on the (user_id, email_ref) unique constraint.
 */
const PUSH_BATCH_SIZE = 100;

async function pushEmails(emails) {
  if (!emails.length) return;

  for (let i = 0; i < emails.length; i += PUSH_BATCH_SIZE) {
    const chunk = emails.slice(i, i + PUSH_BATCH_SIZE);
    await supabaseRequest("/emails?on_conflict=user_id,email_ref", {
      method: "POST",
      headers: {
        Prefer: "resolution=merge-duplicates",
      },
      body: chunk,
    });
    if (DEBUG && emails.length > PUSH_BATCH_SIZE) {
      console.log(`Pushed ${Math.min(i + PUSH_BATCH_SIZE, emails.length)}/${emails.length} emails to Supabase`);
    }
  }
}

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------

/**
 * Fetch the current user's profile (user_email_aliases, etc).
 */
async function getProfile(userId) {
  return supabaseRequest(`/profiles?id=eq.${userId}&select=user_email_aliases`);
}

/**
 * Add new aliases to user_email_aliases (merge, no duplicates).
 */
async function patchProfileAliases(userId, aliases) {
  return supabaseRequest(`/profiles?id=eq.${userId}`, {
    method: "PATCH",
    body: { user_email_aliases: aliases },
  });
}

/**
 * Update a draft's status and optionally set the outlook_draft_id.
 */
async function updateDraftStatus(draftId, status, outlookDraftId) {
  const body = {
    status,
    updated_at: new Date().toISOString(),
  };
  if (outlookDraftId) body.outlook_draft_id = outlookDraftId;

  return supabaseRequest(`/drafts?id=eq.${draftId}`, {
    method: "PATCH",
    body,
  });
}

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

/**
 * Update the user's heartbeat timestamp and timezone on their profile.
 * Called after every sync cycle so the worker knows the user is active.
 */
async function updateHeartbeat(userId) {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";
  return supabaseRequest(`/profiles?id=eq.${userId}`, {
    method: "PATCH",
    body: {
      last_heartbeat_at: new Date().toISOString(),
      timezone: tz,
    },
  });
}
