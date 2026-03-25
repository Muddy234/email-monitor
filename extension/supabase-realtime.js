/**
 * Supabase Realtime — Phoenix Channels WebSocket client.
 * Listens for new drafts and writes them to Outlook as draft emails.
 *
 * Loaded via importScripts() in the service worker.
 * Depends on: supabase-config.js, supabase-auth.js, supabase-rest.js
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let realtimeWs = null;
let realtimeRef = 0;
let realtimeHeartbeatTimer = null;
let realtimeUserId = null;
let staleSweepInProgress = false;

// ---------------------------------------------------------------------------
// Phoenix Channels protocol helpers
// ---------------------------------------------------------------------------

function rtSend(topic, event, payload = {}) {
  if (!realtimeWs || realtimeWs.readyState !== WebSocket.OPEN) return;
  realtimeRef++;
  realtimeWs.send(JSON.stringify({
    topic,
    event,
    payload,
    ref: String(realtimeRef),
  }));
}

function startHeartbeat() {
  stopHeartbeat();
  realtimeHeartbeatTimer = setInterval(() => {
    rtSend("phoenix", "heartbeat");
  }, 30_000);
}

function stopHeartbeat() {
  if (realtimeHeartbeatTimer) {
    clearInterval(realtimeHeartbeatTimer);
    realtimeHeartbeatTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Draft helpers
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatEmailDate(isoString) {
  if (!isoString) return "";
  try {
    const d = new Date(isoString);
    return d.toLocaleString("en-US", {
      weekday: "long", year: "numeric", month: "long", day: "numeric",
      hour: "numeric", minute: "2-digit", hour12: true,
    });
  } catch (_) {
    return isoString;
  }
}

function buildReplyAllRecipients(parentEmail, userAliases) {
  const seen = new Set();
  const recipients = parentEmail.recipients || [];

  const toList = [];
  const ccList = [];

  // Add original sender to To
  const senderAddr = (parentEmail.sender_email || "").toLowerCase();
  if (senderAddr && !userAliases.has(senderAddr)) {
    seen.add(senderAddr);
    toList.push({ name: parentEmail.sender_name || "", address: parentEmail.sender_email });
  }

  // Add original To recipients (type 1), excluding user aliases and dupes
  for (const r of recipients) {
    const addr = (r.address || "").toLowerCase();
    if (r.type === 1 && addr && !userAliases.has(addr) && !seen.has(addr)) {
      seen.add(addr);
      toList.push({ name: r.name || "", address: r.address });
    }
  }

  // Add original CC recipients (type 2), excluding user aliases and dupes
  for (const r of recipients) {
    const addr = (r.address || "").toLowerCase();
    if (r.type === 2 && addr && !userAliases.has(addr) && !seen.has(addr)) {
      seen.add(addr);
      ccList.push({ name: r.name || "", address: r.address });
    }
  }

  return { toRecipients: toList, ccRecipients: ccList };
}

function buildThreadedBody(draftBody, parentEmail) {
  const escapedDraft = escapeHtml(draftBody).replace(/\n/g, "<br>");
  const escapedBody = escapeHtml(parentEmail.body || "").replace(/\n/g, "<br>");
  const date = formatEmailDate(parentEmail.received_time);

  let header = `<b>From:</b> ${escapeHtml(parentEmail.sender_name || "")} &lt;${escapeHtml(parentEmail.sender_email || "")}&gt;<br>`;
  header += `<b>Sent:</b> ${escapeHtml(date)}<br>`;
  if (parentEmail.to_field) header += `<b>To:</b> ${escapeHtml(parentEmail.to_field)}<br>`;
  if (parentEmail.cc_field) header += `<b>Cc:</b> ${escapeHtml(parentEmail.cc_field)}<br>`;
  header += `<b>Subject:</b> ${escapeHtml(parentEmail.subject || "")}`;

  return `<div>${escapedDraft}</div><br><div style="border-top:1px solid #ccc;padding-top:10px;margin-top:10px;color:#666;">${header}<br><br>${escapedBody}</div>`;
}

// ---------------------------------------------------------------------------
// Draft handler — called when a new pending draft arrives
// ---------------------------------------------------------------------------

async function handleNewDraft(record) {
  try {
    const draftId = record.id;
    const emailId = record.email_id;
    const draftBody = record.draft_body;

    if (!draftBody || record.status !== "pending") return;

    // Fetch user aliases to exclude from reply-all
    let userAliases = new Set();
    try {
      const profiles = await getProfile(realtimeUserId);
      const aliases = profiles?.[0]?.user_email_aliases || [];
      userAliases = new Set(aliases.map(a => a.toLowerCase()));
    } catch (_) {
      if (DEBUG) console.warn("Realtime: could not fetch user aliases, proceeding without");
    }

    // Fetch the parent email with full recipient and body data
    const emails = await supabaseRequest(
      `/emails?id=eq.${emailId}&select=email_ref,sender_email,sender_name,subject,recipients,to_field,cc_field,body,received_time`
    );
    const parentEmail = emails?.[0];
    if (!parentEmail) {
      if (DEBUG) console.warn("Realtime: parent email not found for draft", draftId);
      return;
    }

    // Build reply-all recipients and threaded HTML body
    let { toRecipients, ccRecipients } = buildReplyAllRecipients(parentEmail, userAliases);

    // Fallback: if To is empty, reply directly to the original sender
    if (toRecipients.length === 0 && parentEmail.sender_email) {
      toRecipients = [{ name: parentEmail.sender_name || "", address: parentEmail.sender_email }];
    }

    const htmlBody = buildThreadedBody(draftBody, parentEmail);

    const subject = parentEmail.subject?.startsWith("Re: ")
      ? parentEmail.subject
      : `Re: ${parentEmail.subject || ""}`;

    const result = await handleSaveDraft({
      subject,
      body: htmlBody,
      to_recipients: toRecipients,
      cc_recipients: ccRecipients,
      body_type: "HTML",
    });

    if (result.success) {
      await updateDraftStatus(draftId, "written", result.draft_ref);
      if (DEBUG) console.log("Realtime: draft written to Outlook:", subject);
    } else {
      if (DEBUG) console.error("Realtime: failed to save draft to Outlook");
    }
  } catch (err) {
    if (DEBUG) console.error("Realtime: error handling draft:", err.message);
  }
}

// ---------------------------------------------------------------------------
// Connect / disconnect
// ---------------------------------------------------------------------------

function connectRealtime(userId, accessToken) {
  disconnectRealtime();
  realtimeUserId = userId;

  const projectRef = new URL(SUPABASE_URL).hostname.split(".")[0];
  const wsUrl = `wss://${projectRef}.supabase.co/realtime/v1/websocket?apikey=${SUPABASE_ANON_KEY}&vsn=1.0.0`;

  try {
    realtimeWs = new WebSocket(wsUrl);
  } catch (_) {
    realtimeWs = null;
    return;
  }

  realtimeWs.onopen = () => {
    if (DEBUG) console.log("Realtime: connected");
    startHeartbeat();

    // Join the drafts channel filtered to this user
    const topic = `realtime:public:drafts:user_id=eq.${userId}`;
    rtSend(topic, "phx_join", {
      user_token: accessToken,
    });
  };

  realtimeWs.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (_) {
      return;
    }

    // Handle INSERT events on the drafts table
    if (msg.event === "INSERT" && msg.payload?.record) {
      handleNewDraft(msg.payload.record);
    }
  };

  realtimeWs.onclose = () => {
    if (DEBUG) console.log("Realtime: disconnected");
    stopHeartbeat();
    realtimeWs = null;
  };

  realtimeWs.onerror = () => {
    stopHeartbeat();
    realtimeWs = null;
  };
}

function disconnectRealtime() {
  stopHeartbeat();
  if (realtimeWs) {
    try { realtimeWs.close(); } catch (_) {}
    realtimeWs = null;
  }
  realtimeUserId = null;
}

function isRealtimeConnected() {
  return realtimeWs && realtimeWs.readyState === WebSocket.OPEN;
}

// ---------------------------------------------------------------------------
// Pending-draft sweep — polling fallback for missed Realtime events
// ---------------------------------------------------------------------------

/**
 * Query Supabase for drafts stuck at "pending" with no outlook_draft_id
 * and process each through the normal handleNewDraft flow.
 * Called on every alarm tick to recover drafts missed while the
 * MV3 service worker was suspended (WebSocket dead).
 */
async function sweepPendingDrafts(userId) {
  try {
    const drafts = await supabaseRequest(
      `/drafts?user_id=eq.${userId}&status=eq.pending&outlook_draft_id=is.null&select=id,email_id,draft_body,status&order=created_at.asc`
    );
    if (!drafts || drafts.length === 0) return 0;

    if (DEBUG) console.log(`Sweep: found ${drafts.length} pending draft(s)`);

    // Ensure realtimeUserId is set so handleNewDraft can fetch aliases
    realtimeUserId = userId;

    let delivered = 0;
    for (const draft of drafts) {
      try {
        await handleNewDraft(draft);
        delivered++;
      } catch (err) {
        if (DEBUG) console.warn(`Sweep: failed to deliver draft ${draft.id}:`, err.message);
      }
    }
    if (DEBUG) console.log(`Sweep: delivered ${delivered}/${drafts.length} draft(s)`);
    return delivered;
  } catch (err) {
    if (DEBUG) console.warn("Sweep: query failed:", err.message);
    return 0;
  }
}

// ---------------------------------------------------------------------------
// Stale-draft sweep — delete drafts whose conversation has a sent reply
// ---------------------------------------------------------------------------

/**
 * Find drafts that are stale (user already replied in the same conversation)
 * and delete them from Outlook + mark deleted in Supabase.
 * Runs BEFORE sweepPendingDrafts to prevent writing stale drafts to Outlook.
 */
async function sweepStaleDrafts() {
  if (staleSweepInProgress) return 0;
  staleSweepInProgress = true;

  try {
    const staleDrafts = await supabaseRequest("/rpc/find_stale_drafts", {
      method: "POST",
      body: {},
    });

    if (!staleDrafts || staleDrafts.length === 0) return 0;

    let deleted = 0;
    let skipped = 0;

    for (const draft of staleDrafts) {
      try {
        // Only call OWA DeleteItem for drafts already written to Outlook
        if (draft.status === "written" && draft.outlook_draft_id) {
          await handleDeleteItem(draft.outlook_draft_id);
        }

        // Mark deleted in Supabase regardless of prior status
        await supabaseRequest(`/drafts?id=eq.${draft.draft_id}`, {
          method: "PATCH",
          body: {
            draft_deleted: true,
            status: "deleted",
            updated_at: new Date().toISOString(),
          },
        });

        deleted++;
      } catch (err) {
        skipped++;
        if (DEBUG) console.warn(`Stale sweep: failed draft ${draft.draft_id}:`, err.message);

        // No point trying more OWA calls if token is gone
        if (err.message === "TOKEN_EXPIRED") break;
      }
    }

    if (DEBUG) console.log(`Stale sweep: found=${staleDrafts.length} deleted=${deleted} skipped=${skipped}`);
    return deleted;
  } catch (err) {
    if (DEBUG) console.warn("Stale sweep: query failed:", err.message);
    return 0;
  } finally {
    staleSweepInProgress = false;
  }
}
