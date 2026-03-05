/**
 * Service worker (background) — Email Monitor Outlook Bridge.
 *
 * Responsibilities:
 *  1. Receive + cache MSAL Exchange token from content script.
 *  2. Sync emails to Supabase on a recurring alarm.
 *  3. Execute OWA service.svc commands (FindItem, GetItem, CreateItem).
 *  4. Listen for drafts via Supabase Realtime and write to Outlook.
 *  5. Alarm-driven reconnect for MV3 idle timeout.
 */

// Load Supabase modules (must be synchronous, at top of SW)
importScripts(
  "supabase-config.js",
  "supabase-auth.js",
  "supabase-rest.js",
  "supabase-realtime.js"
);

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const EMAIL_SYNC_ALARM = "email-sync";
const EMAIL_SYNC_PERIOD_MIN = 5;   // 5 min
const MAX_CATCHUP_EMAILS = 500;    // cap for first-time or stale syncs
const MAX_CATCHUP_DAYS = 30;       // how far back to look on first sync

// OWA endpoint templates
const OWA_ENDPOINTS = {
  "outlook.cloud.microsoft": "/owa/service.svc",
  "outlook.live.com": "/owa/0/service.svc",
  "outlook.office365.com": "/owa/service.svc",
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let token = null;        // { token, expiresOn, cachedAt, clientId, origin }
let lastCommand = null;  // { action, timestamp }
let isSyncing = false;   // lock to prevent concurrent Supabase syncs
let lastSyncTime = null; // ISO string of last successful sync

/** Persist lastSyncTime to chrome.storage.local. */
function persistSyncTime() {
  if (lastSyncTime) {
    chrome.storage.local.set({ lastSyncTime });
  }
}

/** Restore lastSyncTime from chrome.storage.local on SW wake. */
async function restoreSyncTime() {
  const result = await chrome.storage.local.get("lastSyncTime");
  if (result.lastSyncTime) lastSyncTime = result.lastSyncTime;
}

// ---------------------------------------------------------------------------
// Badge helpers
// ---------------------------------------------------------------------------

function setBadge(status) {
  const map = {
    ok: { text: "", color: "#22c55e" },          // green — token valid
    no_token: { text: "?", color: "#eab308" },   // yellow — no token
    error: { text: "!", color: "#ef4444" },       // red — token expired
  };
  const cfg = map[status] || map.no_token;
  chrome.action.setBadgeText({ text: cfg.text });
  chrome.action.setBadgeBackgroundColor({ color: cfg.color });
}

function updateBadge() {
  if (!token || !token.token) {
    setBadge("no_token");
  } else if (isTokenExpired()) {
    setBadge("error");
  } else {
    setBadge("ok");
  }
}

// ---------------------------------------------------------------------------
// Token management
// ---------------------------------------------------------------------------

/** Persist token to chrome.storage.session (survives SW restart). */
function persistToken() {
  if (token) {
    chrome.storage.session.set({ exchangeToken: token });
  }
}

/** Restore token from chrome.storage.session on SW wake. */
async function restoreToken() {
  if (token) return; // already have one in memory
  const result = await chrome.storage.session.get("exchangeToken");
  if (result.exchangeToken) {
    token = result.exchangeToken;
  }
}

/** Check if the cached token is expired. */
function isTokenExpired() {
  if (!token || !token.expiresOn) return true;
  return Math.floor(Date.now() / 1000) >= token.expiresOn;
}

// ---------------------------------------------------------------------------
// OWA request helpers
// ---------------------------------------------------------------------------

/** Resolve the service.svc base URL from the token origin. */
function getServiceUrl(action) {
  if (!token || !token.origin) return null;
  try {
    const host = new URL(token.origin).hostname;
    const path = OWA_ENDPOINTS[host] || OWA_ENDPOINTS["outlook.cloud.microsoft"];
    return `${token.origin}${path}?action=${action}&app=Mail`;
  } catch (_) {
    return null;
  }
}

/** Build the standard OWA JSON-RPC request wrapper. */
function wrapRequest(action, body) {
  return {
    __type: `${action}JsonRequest:#Exchange`,
    Header: {
      __type: "JsonRequestHeaders:#Exchange",
      RequestServerVersion: "Exchange2013",
      TimeZoneContext: {
        __type: "TimeZoneContext:#Exchange",
        TimeZoneDefinition: { Id: "Central Standard Time" },
      },
    },
    Body: body,
  };
}

/** Execute a fetch to service.svc with the cached Bearer token. */
async function owaFetch(action, body) {
  const url = getServiceUrl(action);
  if (!url) throw new Error("No OWA endpoint — token origin unknown");
  if (!token || !token.token) throw new Error("No Exchange token available");

  const resp = await fetch(url, {
    method: "POST",
    credentials: "omit",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token.token}`,
      Action: action,
    },
    body: JSON.stringify(wrapRequest(action, body)),
  });

  if (resp.status === 401) {
    // Token expired — mark it and notify
    token.token = null;
    persistToken();
    updateBadge();
    throw new Error("TOKEN_EXPIRED");
  }

  return resp.json();
}

// ---------------------------------------------------------------------------
// Command handlers
// ---------------------------------------------------------------------------

const IMPORTANCE_MAP = { Low: 0, Normal: 1, High: 2 };

/** Parse a FindItem OWA message into an email_data dict. */
function parseFindItemMessage(item, folder) {
  return {
    subject: item.Subject || "",
    sender: item.From?.Mailbox?.EmailAddress || "",
    sender_email: item.From?.Mailbox?.EmailAddress || "",
    sender_name: item.From?.Mailbox?.Name || "",
    received_time: item.DateTimeReceived || "",
    has_attachments: item.HasAttachments || false,
    folder: folder,
    flag_status: item.Flag?.FlagStatus || "NotFlagged",
    conversation_id: item.ConversationId?.Id || null,
    conversation_topic: item.ConversationTopic || null,
    email_ref: item.ItemId?.Id || "",
    importance: IMPORTANCE_MAP[item.Importance] ?? 1,
    is_read: item.IsRead ?? true,
    // Fields only available from GetItem:
    body: "",
    attachment_names: [],
    to_field: "",
    cc_field: "",
    recipients: [],
    // Internal: needed for UpdateItem calls later
    _change_key: item.ItemId?.ChangeKey || "",
  };
}

/** Parse a GetItem OWA message into enriched fields. */
function parseGetItemMessage(item) {
  const toRecips = item.ToRecipients || [];
  const ccRecips = item.CcRecipients || [];
  const attachments = item.Attachments || [];

  const recipients = [
    ...toRecips.map((r) => ({ name: r.Name || "", address: r.EmailAddress || "", type: 1 })),
    ...ccRecips.map((r) => ({ name: r.Name || "", address: r.EmailAddress || "", type: 2 })),
  ];

  return {
    subject: item.Subject || "",
    sender: item.From?.Mailbox?.EmailAddress || "",
    sender_email: item.From?.Mailbox?.EmailAddress || "",
    sender_name: item.From?.Mailbox?.Name || "",
    body: item.Body?.Value || "",
    received_time: item.DateTimeReceived || "",
    has_attachments: item.HasAttachments || false,
    attachment_names: attachments.map((a) => a.Name).filter(Boolean),
    flag_status: item.Flag?.FlagStatus || "NotFlagged",
    conversation_id: item.ConversationId?.Id || null,
    conversation_topic: item.ConversationTopic || null,
    email_ref: item.ItemId?.Id || "",
    importance: IMPORTANCE_MAP[item.Importance] ?? 1,
    to_field: toRecips.map((r) => r.EmailAddress).filter(Boolean).join("; "),
    cc_field: ccRecips.map((r) => r.EmailAddress).filter(Boolean).join("; "),
    recipients: recipients,
    _change_key: item.ItemId?.ChangeKey || "",
  };
}

// --- FindItem (get_emails) -------------------------------------------------

async function handleGetEmails(params) {
  const folder = params.folder || "inbox";
  const maxEntries = params.max_scan || 50;
  const offset = params.offset || 0;
  const flaggedOnly = params.flagged_only || false;

  const properties = [
    { __type: "PropertyUri:#Exchange", FieldURI: "Subject" },
    { __type: "PropertyUri:#Exchange", FieldURI: "DateTimeReceived" },
    { __type: "PropertyUri:#Exchange", FieldURI: "DateTimeSent" },
    { __type: "PropertyUri:#Exchange", FieldURI: "From" },
    { __type: "PropertyUri:#Exchange", FieldURI: "Importance" },
    { __type: "PropertyUri:#Exchange", FieldURI: "HasAttachments" },
    { __type: "PropertyUri:#Exchange", FieldURI: "Flag" },
    { __type: "PropertyUri:#Exchange", FieldURI: "ConversationId" },
    { __type: "PropertyUri:#Exchange", FieldURI: "ConversationTopic" },
    { __type: "PropertyUri:#Exchange", FieldURI: "IsRead" },
  ];

  const body = {
    __type: "FindItemRequest:#Exchange",
    ItemShape: {
      __type: "ItemResponseShape:#Exchange",
      BaseShape: "IdOnly",
      AdditionalProperties: properties,
    },
    ParentFolderIds: [
      { __type: "DistinguishedFolderId:#Exchange", Id: folder },
    ],
    Traversal: "Shallow",
    Paging: {
      __type: "IndexedPageView:#Exchange",
      BasePoint: "Beginning",
      Offset: offset,
      MaxEntriesReturned: maxEntries,
    },
  };

  // Optional: restriction to flagged items only
  if (flaggedOnly) {
    body.Restriction = {
      __type: "IsEqualTo:#Exchange",
      FieldURIOrConstant: { Value: "Flagged" },
      Item: { __type: "PropertyUri:#Exchange", FieldURI: "Flag" },
    };
  }

  // Optional: date filter
  if (params.start_date) {
    body.Restriction = {
      __type: "IsGreaterThanOrEqualTo:#Exchange",
      FieldURIOrConstant: {
        __type: "ConstantValueType:#Exchange",
        Value: params.start_date,
      },
      Item: { __type: "PropertyUri:#Exchange", FieldURI: "DateTimeReceived" },
    };
  }

  const data = await owaFetch("FindItem", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];
  if (!ri || ri.ResponseCode !== "NoError") {
    throw new Error(`FindItem failed: ${ri?.ResponseCode || data.Body?.ErrorCode || "unknown"}`);
  }

  const rootFolder = ri.RootFolder;
  const items = rootFolder?.Items || [];
  const folderName = folder.charAt(0).toUpperCase() + folder.slice(1);

  return {
    emails: items.map((item) => parseFindItemMessage(item, folderName)),
    total: rootFolder?.TotalItemsInView || 0,
    offset: rootFolder?.IndexedPagingOffset || 0,
    includes_last: rootFolder?.IncludesLastItemInRange || false,
  };
}

// --- GetItem (get_item) ----------------------------------------------------

async function handleGetItem(params) {
  const messageId = params.message_id;
  const changeKey = params.change_key || undefined;

  const properties = [
    { __type: "PropertyUri:#Exchange", FieldURI: "Subject" },
    { __type: "PropertyUri:#Exchange", FieldURI: "Body" },
    { __type: "PropertyUri:#Exchange", FieldURI: "From" },
    { __type: "PropertyUri:#Exchange", FieldURI: "ToRecipients" },
    { __type: "PropertyUri:#Exchange", FieldURI: "CcRecipients" },
    { __type: "PropertyUri:#Exchange", FieldURI: "DateTimeReceived" },
    { __type: "PropertyUri:#Exchange", FieldURI: "Importance" },
    { __type: "PropertyUri:#Exchange", FieldURI: "HasAttachments" },
    { __type: "PropertyUri:#Exchange", FieldURI: "Attachments" },
    { __type: "PropertyUri:#Exchange", FieldURI: "Flag" },
    { __type: "PropertyUri:#Exchange", FieldURI: "ConversationId" },
    { __type: "PropertyUri:#Exchange", FieldURI: "ConversationTopic" },
  ];

  const itemId = { __type: "ItemId:#Exchange", Id: messageId };
  if (changeKey) itemId.ChangeKey = changeKey;

  const body = {
    __type: "GetItemRequest:#Exchange",
    ItemShape: {
      __type: "ItemResponseShape:#Exchange",
      BaseShape: "IdOnly",
      AdditionalProperties: properties,
      BodyType: "Text",
    },
    ItemIds: [itemId],
  };

  const data = await owaFetch("GetItem", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];
  if (!ri || ri.ResponseCode !== "NoError") {
    throw new Error(`GetItem failed: ${ri?.ResponseCode || data.Body?.ErrorCode || "unknown"}`);
  }

  const msg = ri.Items?.[0];
  if (!msg) throw new Error("GetItem returned no items");

  return parseGetItemMessage(msg);
}

// --- UpdateItem — unflag email ---------------------------------------------

async function handleUnflagEmail(params) {
  const messageId = params.message_id;
  const changeKey = params.change_key || undefined;
  const flagStatus = params.flag_status || "NotFlagged";

  const itemId = { __type: "ItemId:#Exchange", Id: messageId };
  if (changeKey) itemId.ChangeKey = changeKey;

  const body = {
    __type: "UpdateItemRequest:#Exchange",
    ItemChanges: [
      {
        __type: "ItemChange:#Exchange",
        ItemId: itemId,
        Updates: [
          {
            __type: "SetItemField:#Exchange",
            Item: {
              __type: "Message:#Exchange",
              Flag: { __type: "FlagType:#Exchange", FlagStatus: flagStatus },
            },
            Path: { __type: "PropertyUri:#Exchange", FieldURI: "Flag" },
          },
        ],
      },
    ],
    ConflictResolution: "AlwaysOverwrite",
    MessageDisposition: "SaveOnly",
  };

  const data = await owaFetch("UpdateItem", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];
  if (!ri || ri.ResponseCode !== "NoError") {
    throw new Error(`UpdateItem failed: ${ri?.ResponseCode || data.Body?.ErrorCode || "unknown"}`);
  }

  return {
    success: true,
    new_change_key: ri.Items?.[0]?.ItemId?.ChangeKey || null,
  };
}

// --- CreateItem — save draft -----------------------------------------------

async function handleSaveDraft(params) {
  const subject = params.subject || "";
  const htmlBody = params.body || "";
  const toRecipients = params.to_recipients || [];
  const bodyType = params.body_type || "HTML";

  const items = [
    {
      __type: "Message:#Exchange",
      Subject: subject,
      Body: { BodyType: bodyType, Value: htmlBody },
      ToRecipients: toRecipients.map((r) => ({
        Name: r.name || r.address || "",
        EmailAddress: r.address || r.email || "",
        RoutingType: "SMTP",
      })),
    },
  ];

  const body = {
    __type: "CreateItemRequest:#Exchange",
    Items: items,
    MessageDisposition: "SaveOnly",
  };

  const data = await owaFetch("CreateItem", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];
  if (!ri || ri.ResponseCode !== "NoError") {
    throw new Error(`CreateItem failed: ${ri?.ResponseCode || data.Body?.ErrorCode || "unknown"}`);
  }

  const created = ri.Items?.[0];
  return {
    success: true,
    draft_ref: created?.ItemId?.Id || null,
    change_key: created?.ItemId?.ChangeKey || null,
  };
}

// --- FindItem on Sent Items ------------------------------------------------

async function handleGetSentItems(params) {
  const result = await handleGetEmails({
    ...params,
    folder: "sentitems",
  });
  return result;
}

// --- GetConversationItems --------------------------------------------------

async function handleGetConversationMessages(params) {
  const conversationId = params.conversation_id;
  const maxItems = params.max_items || 20;

  const body = {
    __type: "GetConversationItemsRequest:#Exchange",
    Conversations: [
      {
        __type: "ConversationRequestType:#Exchange",
        ConversationId: { __type: "ItemId:#Exchange", Id: conversationId },
      },
    ],
    ItemShape: {
      __type: "ItemResponseShape:#Exchange",
      BaseShape: "IdOnly",
      AdditionalProperties: [
        { __type: "PropertyUri:#Exchange", FieldURI: "Subject" },
        { __type: "PropertyUri:#Exchange", FieldURI: "Body" },
        { __type: "PropertyUri:#Exchange", FieldURI: "From" },
        { __type: "PropertyUri:#Exchange", FieldURI: "ToRecipients" },
        { __type: "PropertyUri:#Exchange", FieldURI: "CcRecipients" },
        { __type: "PropertyUri:#Exchange", FieldURI: "DateTimeReceived" },
        { __type: "PropertyUri:#Exchange", FieldURI: "Importance" },
        { __type: "PropertyUri:#Exchange", FieldURI: "HasAttachments" },
        { __type: "PropertyUri:#Exchange", FieldURI: "Flag" },
        { __type: "PropertyUri:#Exchange", FieldURI: "ConversationId" },
        { __type: "PropertyUri:#Exchange", FieldURI: "ConversationTopic" },
      ],
      BodyType: "Text",
    },
    MaxItemsToReturn: maxItems,
  };

  const data = await owaFetch("GetConversationItems", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];
  if (!ri || ri.ResponseCode !== "NoError") {
    throw new Error(`GetConversationItems failed: ${ri?.ResponseCode || data.Body?.ErrorCode || "unknown"}`);
  }

  // Conversation response has a different shape — Items within Conversation nodes
  const conversation = ri.Conversation;
  const nodes = conversation?.ConversationNodes || [];
  const messages = [];
  for (const node of nodes) {
    for (const item of node.Items || []) {
      messages.push(parseGetItemMessage(item));
    }
  }
  return { messages };
}

// --- Ping (health check) ---------------------------------------------------

async function handlePing() {
  return {
    ok: true,
    has_token: !!(token && token.token),
    token_expired: isTokenExpired(),
  };
}

// --- Command dispatcher ----------------------------------------------------

const HANDLERS = {
  get_emails: handleGetEmails,
  get_item: handleGetItem,
  unflag_email: handleUnflagEmail,
  save_draft: handleSaveDraft,
  get_sent_items: handleGetSentItems,
  get_conversation_messages: handleGetConversationMessages,
  ping: handlePing,
};

async function dispatchCommand(command) {
  const { action, request_id, ...params } = command;

  if (action === "release") {
    chrome.alarms.clear(ALARM_NAME);
    return { request_id, action, success: true };
  }

  const handler = HANDLERS[action];
  if (!handler) {
    return { request_id, action, error: `Unknown action: ${action}` };
  }

  lastCommand = { action, timestamp: Date.now() };

  try {
    const result = await handler(params);
    return { request_id, action, ...result };
  } catch (err) {
    return { request_id, action, error: err.message };
  }
}


// ---------------------------------------------------------------------------
// Supabase email sync
// ---------------------------------------------------------------------------

/**
 * Detect the user's Outlook email from synced emails and update profile
 * aliases if they're empty. Scans to_field/cc_field across emails to find
 * addresses that appear as recipients (the user's own mailbox).
 */
async function detectAndUpdateAliases(userId, emails) {
  try {
    const profiles = await getProfile(userId);
    const existing = (profiles?.[0]?.user_email_aliases || []).map(a => a.toLowerCase());

    // Collect all recipient addresses from to_field, cc_field, and recipients array
    const candidates = new Map(); // address → count
    const emailRegex = /[\w.+-]+@[\w.-]+\.\w+/g;
    for (const e of emails) {
      const found = new Set();
      // Parse from structured recipients
      for (const r of (e.recipients || [])) {
        if (r.address) found.add(r.address.toLowerCase());
      }
      // Parse from to_field / cc_field strings as fallback
      for (const field of [e.to_field, e.cc_field]) {
        if (!field) continue;
        for (const match of field.matchAll(emailRegex)) {
          found.add(match[0].toLowerCase());
        }
      }
      // Exclude the sender — we want recipient-only addresses
      const sender = (e.sender_email || e.sender || "").toLowerCase();
      found.delete(sender);
      for (const addr of found) {
        candidates.set(addr, (candidates.get(addr) || 0) + 1);
      }
    }

    // The user's own address is the most frequent non-sender recipient.
    // Pick addresses appearing in at least 3 emails or 20% of the batch.
    const threshold = Math.max(3, Math.floor(emails.length * 0.2));
    const detected = [];
    for (const [addr, count] of candidates) {
      if (count >= threshold) detected.push(addr);
    }

    if (DEBUG) console.log(`Alias detection: ${emails.length} emails, ${candidates.size} unique recipients, ${detected.length} above threshold (${threshold})`);
    if (detected.length === 0) return;

    // Merge with existing aliases (no duplicates)
    const merged = [...new Set([...existing, ...detected])];
    if (merged.length === existing.length) return; // nothing new

    await patchProfileAliases(userId, merged);
    if (DEBUG) console.log("Updated user aliases:", merged);
  } catch (err) {
    if (DEBUG) console.warn("Alias detection failed (non-blocking):", err.message);
  }
}

async function syncEmailsToSupabase() {
  if (isSyncing) return { skipped: true };
  isSyncing = true;

  try {
    // Check both tokens exist
    if (!token || !token.token || isTokenExpired()) {
      return { error: "No valid Outlook token" };
    }

    const session = await getSupabaseSession();
    if (!session || !session.access_token) {
      return { error: "Not logged in to Supabase" };
    }

    const userId = session.user.id;

    // Set limit: small for incremental syncs, larger for catch-up
    const maxEmails = lastSyncTime ? 50 : MAX_CATCHUP_EMAILS;

    // Fetch emails via OWA (no date filter — upsert handles duplicates)
    const result = await handleGetEmails({
      folder: "inbox",
      max_scan: maxEmails,
      flagged_only: false,
    });

    if (!result.emails || result.emails.length === 0) {
      lastSyncTime = new Date().toISOString();
      persistSyncTime();
      return { synced: 0 };
    }

    // Enrich each email with body/recipients via GetItem
    const enriched = [];
    for (const email of result.emails) {
      try {
        const detail = await handleGetItem({
          message_id: email.email_ref,
          change_key: email._change_key,
        });
        enriched.push(detail);
      } catch (err) {
        // If GetItem fails for one email, still push the basic info
        enriched.push(email);
      }
    }

    // Transform to Supabase row format
    const rows = enriched.map((e) => ({
      user_id: userId,
      email_ref: e.email_ref,
      subject: e.subject || "",
      sender: e.sender || "",
      sender_name: e.sender_name || "",
      sender_email: e.sender_email || "",
      received_time: e.received_time || null,
      body: (e.body || "").slice(0, 50000), // cap body size
      has_attachments: e.has_attachments || false,
      attachment_names: e.attachment_names || [],
      folder: e.folder || "Inbox",
      flag_status: e.flag_status || "NotFlagged",
      conversation_id: e.conversation_id || null,
      conversation_topic: e.conversation_topic || null,
      to_field: e.to_field || "",
      cc_field: e.cc_field || "",
      importance: ["Low", "Normal", "High"][e.importance] || "Normal",
      recipients: e.recipients || [],
      status: "unprocessed",
    }));

    // Upsert to Supabase
    await pushEmails(rows);
    lastSyncTime = new Date().toISOString();
    persistSyncTime();
    if (DEBUG) console.log(`Synced ${rows.length} emails to Supabase`);

    // Auto-detect user's Outlook email and update profile aliases if needed
    await detectAndUpdateAliases(userId, enriched);

    return { synced: rows.length };
  } catch (err) {
    if (DEBUG) console.error("Email sync error:", err.message);
    return { error: err.message };
  } finally {
    isSyncing = false;
  }
}

/**
 * Initialize Supabase features — start sync alarm + Realtime.
 * Called on startup and when session changes.
 */
async function initSupabase() {
  const session = await getSupabaseSession();
  if (!session || !session.access_token) {
    disconnectRealtime();
    return;
  }

  // Start email sync alarm
  chrome.alarms.create(EMAIL_SYNC_ALARM, { periodInMinutes: EMAIL_SYNC_PERIOD_MIN });

  // Connect Realtime for draft listening
  if (!isRealtimeConnected()) {
    const accessToken = await getValidAccessToken();
    if (accessToken) {
      connectRealtime(session.user.id, accessToken);
    }
  }
}

// ---------------------------------------------------------------------------
// Alarm-driven email sync
// ---------------------------------------------------------------------------

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === EMAIL_SYNC_ALARM) {
    syncEmailsToSupabase();
  }
});

// ---------------------------------------------------------------------------
// Message listener — receive tokens from content script
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "token_update" && msg.data) {
    token = msg.data;
    persistToken();
    updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "token_update" && !msg.data) {
    // Content script found no token
    if (!token || !token.token) updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "getStatus") {
    sendResponse({
      has_token: !!(token && token.token),
      token_expired: isTokenExpired(),
      token_origin: token?.origin || null,
      token_expires: token?.expiresOn
        ? new Date(token.expiresOn * 1000).toISOString()
        : null,
      last_command: lastCommand,
      // Supabase state
      last_sync: lastSyncTime,
      realtime_connected: isRealtimeConnected(),
      is_syncing: isSyncing,
    });
  } else if (msg.type === "supabaseSessionChanged") {
    initSupabase();
    sendResponse({ ok: true });
  } else if (msg.type === "syncNow") {
    // Manual sync triggered from popup
    syncEmailsToSupabase().then((result) => {
      sendResponse(result);
    });
    return true; // async response
  }
  return false; // synchronous response
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

(async () => {
  await restoreToken();
  await restoreSyncTime();
  updateBadge();

  // Initialize Supabase features (sync alarm + Realtime)
  await initSupabase();
})();
