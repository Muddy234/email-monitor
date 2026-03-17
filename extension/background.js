/**
 * Service worker (background) — Clarion AI.
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
const EMAIL_SYNC_PERIOD_MIN = 2;   // 2 min
const MAX_CATCHUP_EMAILS = 6000;   // cap for first-time or stale syncs
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
let isSyncing = false;   // lock to prevent concurrent Supabase syncs
let lastSyncTime = null; // ISO string of last successful sync
let cachedFolders = null;      // Array of { id, displayName, isDistinguished }
let folderCacheTime = null;    // ISO timestamp of last folder discovery
const FOLDER_CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour

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
    ok:       { text: "",  color: "#22c55e", title: "Clarion AI — Syncing" },
    no_token: { text: "?", color: "#eab308", title: "Clarion AI — Open Outlook to connect" },
    error:    { text: "!", color: "#ef4444", title: "Clarion AI — Token expired, reopen Outlook" },
  };
  const cfg = map[status] || map.no_token;
  chrome.action.setBadgeText({ text: cfg.text });
  chrome.action.setBadgeBackgroundColor({ color: cfg.color });
  chrome.action.setTitle({ title: cfg.title });
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
      params.folderId
        ? { __type: "FolderId:#Exchange", Id: params.folderId }
        : { __type: "DistinguishedFolderId:#Exchange", Id: folder },
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
  const folderName = params.folderDisplayName || (folder.charAt(0).toUpperCase() + folder.slice(1));

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

// --- GetItem batch — fetch multiple emails in one request -------------------

const GETITEM_BATCH_SIZE = 50; // OWA handles up to ~50-100 items per request

async function handleGetItemBatch(emails) {
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

  const itemIds = emails.map((e) => {
    const id = { __type: "ItemId:#Exchange", Id: e.email_ref };
    if (e._change_key) id.ChangeKey = e._change_key;
    return id;
  });

  const body = {
    __type: "GetItemRequest:#Exchange",
    ItemShape: {
      __type: "ItemResponseShape:#Exchange",
      BaseShape: "IdOnly",
      AdditionalProperties: properties,
      BodyType: "Text",
    },
    ItemIds: itemIds,
  };

  const data = await owaFetch("GetItem", body);
  const items = data.Body?.ResponseMessages?.Items || [];

  const results = [];
  for (let i = 0; i < items.length; i++) {
    const ri = items[i];
    if (ri?.ResponseCode === "NoError" && ri.Items?.[0]) {
      results.push(parseGetItemMessage(ri.Items[0]));
    } else {
      // Fall back to basic FindItem data for failed items
      results.push(emails[i]);
    }
  }
  return results;
}

/**
 * Enrich a list of emails by fetching full details in batches.
 * Falls back to basic FindItem data for any items that fail.
 */
async function enrichEmailsBatched(emails) {
  const enriched = [];
  for (let i = 0; i < emails.length; i += GETITEM_BATCH_SIZE) {
    const chunk = emails.slice(i, i + GETITEM_BATCH_SIZE);
    try {
      const batchResults = await handleGetItemBatch(chunk);
      enriched.push(...batchResults);
    } catch (err) {
      if (DEBUG) console.warn(`GetItem batch failed at offset ${i}, falling back to basic data:`, err.message);
      enriched.push(...chunk);
    }
    if (DEBUG && emails.length > GETITEM_BATCH_SIZE) {
      console.log(`Enriched ${Math.min(i + GETITEM_BATCH_SIZE, emails.length)}/${emails.length} emails`);
    }
  }
  return enriched;
}

// --- CreateItem — save draft -----------------------------------------------

async function handleSaveDraft(params) {
  const subject = params.subject || "";
  const htmlBody = params.body || "";
  const toRecipients = params.to_recipients || [];
  const ccRecipients = params.cc_recipients || [];
  const bodyType = params.body_type || "HTML";

  const mapRecipient = (r) => ({
    Name: r.name || r.address || "",
    EmailAddress: r.address || r.email || "",
    RoutingType: "SMTP",
  });

  const message = {
    __type: "Message:#Exchange",
    Subject: subject,
    Body: { BodyType: bodyType, Value: htmlBody },
    ToRecipients: toRecipients.map(mapRecipient),
  };

  if (ccRecipients.length > 0) {
    message.CcRecipients = ccRecipients.map(mapRecipient);
  }

  const body = {
    __type: "CreateItemRequest:#Exchange",
    Items: [message],
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

// ---------------------------------------------------------------------------
// Folder discovery
// ---------------------------------------------------------------------------

const SKIP_FOLDER_NAMES = new Set([
  "sent items", "drafts", "deleted items", "junk email",
  "outbox", "conversation history", "archive",
  "sync issues", "rss feeds", "rss subscriptions",
]);

async function discoverMailFolders() {
  // Return cache if still fresh
  if (cachedFolders && folderCacheTime) {
    const age = Date.now() - new Date(folderCacheTime).getTime();
    if (age < FOLDER_CACHE_TTL_MS) return cachedFolders;
  }

  const body = {
    __type: "FindFolderRequest:#Exchange",
    FolderShape: {
      __type: "FolderResponseShape:#Exchange",
      BaseShape: "Default",
    },
    ParentFolderIds: [
      { __type: "DistinguishedFolderId:#Exchange", Id: "msgfolderroot" },
    ],
    Traversal: "Deep",
  };

  const data = await owaFetch("FindFolder", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];
  if (!ri || ri.ResponseCode !== "NoError") {
    throw new Error(`FindFolder failed: ${ri?.ResponseCode || "unknown"}`);
  }

  const allFolders = ri.RootFolder?.Folders || [];
  const mailFolders = allFolders
    .filter(f => f.FolderClass === "IPF.Note" || !f.FolderClass)
    .filter(f => !SKIP_FOLDER_NAMES.has((f.DisplayName || "").toLowerCase()))
    .map(f => ({
      id: f.FolderId?.Id,
      displayName: f.DisplayName,
      isDistinguished: (f.DisplayName || "").toLowerCase() === "inbox",
    }));

  cachedFolders = mailFolders;
  folderCacheTime = new Date().toISOString();
  chrome.storage.local.set({ cachedFolders, folderCacheTime });

  if (DEBUG) console.log("Discovered mail folders:", mailFolders.map(f => f.displayName));
  return mailFolders;
}

// ---------------------------------------------------------------------------
// Supabase email sync
// ---------------------------------------------------------------------------

/**
 * Detect the user's email aliases from sent items and auth email.
 * Uses the From field of sent emails as the definitive source —
 * only addresses the user actually sent from are true aliases.
 */
async function detectAndUpdateAliases(userId, authEmail, sentEmails) {
  try {
    const profiles = await getProfile(userId);
    const existing = (profiles?.[0]?.user_email_aliases || []).map(a => a.toLowerCase());

    // Collect unique sender addresses from sent items — these are definitively the user's
    const detected = new Set();
    if (authEmail) detected.add(authEmail.toLowerCase());
    for (const e of sentEmails) {
      const addr = (e.sender_email || "").toLowerCase();
      if (addr) detected.add(addr);
    }

    if (DEBUG) console.log(`Alias detection: ${sentEmails.length} sent emails, ${detected.size} aliases found:`, [...detected]);
    if (detected.size === 0) return;

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

    // Discover all mail folders (cached for 1 hour)
    let folders;
    try {
      folders = await discoverMailFolders();
    } catch (err) {
      if (DEBUG) console.warn("Folder discovery failed, falling back to inbox-only:", err.message);
      folders = [{ id: null, displayName: "Inbox", isDistinguished: true }];
    }

    // Loop through each mail folder sequentially
    let totalSynced = 0;
    for (const folderInfo of folders) {
      try {
        // Build fetch params — use raw folderId for non-distinguished folders
        const fetchParams = {
          max_scan: maxEmails,
          flagged_only: false,
        };
        if (folderInfo.isDistinguished) {
          fetchParams.folder = "inbox";
        } else {
          fetchParams.folderId = folderInfo.id;
          fetchParams.folderDisplayName = folderInfo.displayName;
        }

        const result = await handleGetEmails(fetchParams);

        if (!result.emails || result.emails.length === 0) {
          if (DEBUG) console.log(`No emails found in folder "${folderInfo.displayName}"`);
          continue;
        }

        // Enrich emails with body/recipients via batched GetItem
        const enriched = await enrichEmailsBatched(result.emails);

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
          folder: e.folder || folderInfo.displayName,
          flag_status: e.flag_status || "NotFlagged",
          conversation_id: e.conversation_id || null,
          conversation_topic: e.conversation_topic || null,
          to_field: e.to_field || "",
          cc_field: e.cc_field || "",
          importance: ["Low", "Normal", "High"][e.importance] || "Normal",
          recipients: e.recipients || [],
          // NOTE: status is intentionally omitted here. The DB column defaults to
          // "unprocessed" on INSERT, and merge-duplicates upsert must NOT overwrite
          // status on existing rows (which may already be processing/completed).
        }));

        await pushEmails(rows);
        totalSynced += rows.length;
        if (DEBUG) console.log(`Synced ${rows.length} emails from "${folderInfo.displayName}"`);
      } catch (err) {
        // Per-folder errors are non-blocking — log and continue to next folder
        if (DEBUG) console.error(`Error syncing folder "${folderInfo.displayName}":`, err.message);
      }
    }

    // Sync sent items every cycle (incremental for subsequent syncs)
    let sentCount = 0;
    let sentEnriched = [];
    const maxSentEmails = lastSyncTime ? 50 : MAX_CATCHUP_EMAILS;
    try {
      const sentResult = await handleGetSentItems({
        max_scan: maxSentEmails,
        flagged_only: false,
      });

      if (sentResult.emails && sentResult.emails.length > 0) {
        // Enrich sent emails with body/recipients via batched GetItem
        sentEnriched = await enrichEmailsBatched(sentResult.emails);

        // Transform sent items — status is "completed" (not queued for classification)
        const sentRows = sentEnriched.map((e) => ({
          user_id: userId,
          email_ref: e.email_ref,
          subject: e.subject || "",
          sender: e.sender || "",
          sender_name: e.sender_name || "",
          sender_email: e.sender_email || "",
          received_time: e.received_time || null,
          body: (e.body || "").slice(0, 50000),
          has_attachments: e.has_attachments || false,
          attachment_names: e.attachment_names || [],
          folder: "Sent Items",
          flag_status: e.flag_status || "NotFlagged",
          conversation_id: e.conversation_id || null,
          conversation_topic: e.conversation_topic || null,
          to_field: e.to_field || "",
          cc_field: e.cc_field || "",
          importance: ["Low", "Normal", "High"][e.importance] || "Normal",
          recipients: e.recipients || [],
          status: "completed",
        }));

        await pushEmails(sentRows);
        sentCount = sentRows.length;
        if (DEBUG) console.log(`Synced ${sentCount} sent emails to Supabase`);
      }
    } catch (err) {
      // Sent item sync failure is non-blocking — inbox sync already succeeded
      if (DEBUG) console.error("Sent items sync error:", err.message);
    }

    lastSyncTime = new Date().toISOString();
    persistSyncTime();

    // Auto-detect user's Outlook email aliases from sent items + auth email
    const authEmail = session.user?.email || "";
    await detectAndUpdateAliases(userId, authEmail, sentEnriched);

    // Update heartbeat so the worker knows we're active
    updateHeartbeat(userId).catch(() => {});

    return { synced: totalSynced, sent_synced: sentCount };
  } catch (err) {
    if (DEBUG) console.error("Email sync error:", err.message);
    // Surface network/server errors distinctly
    if (err instanceof TypeError || /Failed to fetch|NetworkError/i.test(err.message)) {
      return { error: "Failed to fetch" };
    }
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
    (async () => {
      // 1. Sync emails
      try {
        await syncEmailsToSupabase();
      } catch (err) {
        if (DEBUG) console.error("Alarm sync error:", err.message);
      }

      // 2. Reconnect Realtime if the WebSocket dropped (MV3 idle kill)
      const session = await getSupabaseSession();
      if (session?.access_token && !isRealtimeConnected()) {
        try {
          const accessToken = await getValidAccessToken();
          if (accessToken) connectRealtime(session.user.id, accessToken);
        } catch (_) {}
      }

      // 3. Sweep pending drafts missed while WebSocket was dead
      if (session?.user?.id && token?.token && !isTokenExpired()) {
        try {
          await sweepPendingDrafts(session.user.id);
        } catch (err) {
          if (DEBUG) console.error("Alarm sweep error:", err.message);
        }
      }
    })();
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
      // Supabase state
      last_sync: lastSyncTime,
      realtime_connected: isRealtimeConnected(),
      is_syncing: isSyncing,
    });
  } else if (msg.type === "supabaseSessionChanged") {
    // Clear cached token and folder cache if session was removed (logout)
    chrome.storage.local.get("supabaseSession", (result) => {
      if (!result.supabaseSession) {
        token = null;
        lastSyncTime = null;
        cachedFolders = null;
        folderCacheTime = null;
        chrome.storage.local.remove(["cachedFolders", "folderCacheTime"]);
        updateBadge();
      }
    });
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

  // Restore folder cache from storage
  const folderData = await chrome.storage.local.get(["cachedFolders", "folderCacheTime"]);
  if (folderData.cachedFolders) {
    cachedFolders = folderData.cachedFolders;
    folderCacheTime = folderData.folderCacheTime || null;
  }

  updateBadge();

  // Initialize Supabase features (sync alarm + Realtime)
  await initSupabase();

  // Send initial heartbeat if logged in
  const initSession = await getSupabaseSession();
  if (initSession?.user?.id) {
    updateHeartbeat(initSession.user.id).catch(() => {});
  }
})();
