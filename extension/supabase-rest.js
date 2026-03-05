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
async function pushEmails(emails) {
  if (!emails.length) return;

  return supabaseRequest("/emails?on_conflict=user_id,email_ref", {
    method: "POST",
    headers: {
      Prefer: "resolution=merge-duplicates",
    },
    body: emails,
  });
}

// ---------------------------------------------------------------------------
// Drafts
// ---------------------------------------------------------------------------

/**
 * Get pending drafts for the current user, with parent email info.
 */
async function getPendingDrafts(userId) {
  const path = `/drafts?user_id=eq.${userId}&status=eq.pending&select=*,emails(email_ref,sender_email,sender_name,subject)`;
  return supabaseRequest(path);
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
