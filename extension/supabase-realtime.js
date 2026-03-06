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
// Draft handler — called when a new pending draft arrives
// ---------------------------------------------------------------------------

async function handleNewDraft(record) {
  try {
    const draftId = record.id;
    const emailId = record.email_id;
    const draftBody = record.draft_body;

    if (!draftBody || record.status !== "pending") return;

    // Fetch the parent email to get sender info for the reply
    const emails = await supabaseRequest(`/emails?id=eq.${emailId}&select=email_ref,sender_email,sender_name,subject`);
    const parentEmail = emails?.[0];
    if (!parentEmail) {
      if (DEBUG) console.warn("Realtime: parent email not found for draft", draftId);
      return;
    }

    // Build reply recipients
    const toRecipients = [{
      name: parentEmail.sender_name || "",
      address: parentEmail.sender_email || "",
    }];

    // Create draft in Outlook via OWA
    const subject = parentEmail.subject?.startsWith("Re: ")
      ? parentEmail.subject
      : `Re: ${parentEmail.subject || ""}`;

    const result = await handleSaveDraft({
      subject,
      body: draftBody,
      to_recipients: toRecipients,
      body_type: "Text",
    });

    if (result.success) {
      // Update draft status in Supabase
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
  const wsUrl = `wss://${projectRef}.supabase.co/realtime/v1/websocket?apikey=${accessToken}&vsn=1.0.0`;

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
