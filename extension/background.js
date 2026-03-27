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
const EMAIL_SYNC_PERIOD_MIN = 0.75; // 45 seconds (self-rescheduling)
const MAX_CATCHUP_EMAILS = 6000;   // cap for first-time or stale syncs
// OWA endpoint templates
const OWA_ENDPOINTS = {
  "outlook.cloud.microsoft": "/owa/service.svc",
  "outlook.live.com": "/owa/service.svc",
  "outlook.office365.com": "/owa/service.svc",
};
// Personal Outlook (outlook.live.com) uses cookie auth, not Bearer tokens.
const COOKIE_AUTH_HOSTS = new Set(["outlook.live.com"]);

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let token = null;        // { token, expiresOn, cachedAt, clientId, origin }
let isSyncing = false;   // lock to prevent concurrent Supabase syncs
let lastSyncTime = null; // ISO string of last successful sync
let cachedFolders = null;      // Array of { id, displayName, isDistinguished }
let folderCacheTime = null;    // ISO timestamp of last folder discovery
let outlookTabId = null;       // Tab ID of the active Outlook page (from content script)
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
  const now = Math.floor(Date.now() / 1000);
  const expired = now >= token.expiresOn;
  return expired;
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

/** Check whether the current token origin requires cookie-based auth. */
function usesCookieAuth() {
  if (!token || !token.origin) return false;
  try {
    return COOKIE_AUTH_HOSTS.has(new URL(token.origin).hostname);
  } catch (_) {
    return false;
  }
}

/**
 * Execute an OWA fetch via the Outlook tab's page context (cookie auth).
 * Used for personal accounts (outlook.live.com) where Bearer tokens don't work.
 */
async function owaFetchViaTab(action, body) {
  if (!outlookTabId) throw new Error("No Outlook tab available for OWA request");
  const url = getServiceUrl(action);
  if (!url) throw new Error("No OWA endpoint — token origin unknown");
  if (!token || !token.token) throw new Error("No Exchange token available");

  const payload = JSON.stringify(wrapRequest(action, body));
  const bearerToken = token.token;

  const results = await chrome.scripting.executeScript({
    target: { tabId: outlookTabId },
    world: "MAIN",
    func: async (fetchUrl, fetchAction, fetchBody, authToken) => {
      try {
        const resp = await fetch(fetchUrl, {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            "Action": fetchAction,
            "Authorization": "Bearer " + authToken,
          },
          body: fetchBody,
        });
        if (!resp.ok) {
          const text = await resp.text().catch(() => "");
          return { error: true, status: resp.status, detail: text.substring(0, 500) };
        }
        return { ok: true, data: await resp.json() };
      } catch (err) {
        return { error: true, status: 0, detail: err.message };
      }
    },
    args: [url, action, payload, bearerToken],
  });

  const result = results?.[0]?.result;
  if (!result) throw new Error("Tab OWA fetch returned no result");
  if (result.error) {
    if (result.status === 401 || result.status === 440) {
      token.token = null;
      persistToken();
      updateBadge();
      throw new Error("TOKEN_EXPIRED");
    }
    throw new Error(`OWA ${result.status}: ${result.detail || "unknown"}`);
  }
  return result.data;
}

/** Execute a fetch to service.svc with the cached Bearer token. */
async function owaFetch(action, body) {
  // Personal accounts need cookies + Bearer token — route through the Outlook tab
  if (usesCookieAuth()) {
    return owaFetchViaTab(action, body);
  }

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

  if (!resp.ok) {
    let detail = `OWA ${resp.status}`;
    try {
      const body = await resp.text();
      // OWA sometimes returns JSON errors, sometimes HTML — extract what we can
      if (body.startsWith("{")) {
        const json = JSON.parse(body);
        detail = json.ErrorCode || json.message || detail;
      }
    } catch (_) {}
    throw new Error(detail);
  }

  return resp.json();
}

// ---------------------------------------------------------------------------
// Outlook account locking — extract email from MSAL JWT
// ---------------------------------------------------------------------------

/** Decode the payload section of a JWT (no signature verification). */
function decodeJwtPayload(token) {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    // base64url → base64 → decode
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = atob(b64);
    return JSON.parse(json);
  } catch (_) {
    return null;
  }
}

/**
 * Extract the Outlook email address from the Exchange access token.
 * Tries JWT claims first (preferred_username, upn, unique_name, smtp),
 * then falls back to reading the email from the Outlook tab DOM.
 */
async function getOutlookEmail(accessToken) {
  // Basic email validation — reject truncated/garbage values
  const looksLikeEmail = (s) => /^[^\s@]{2,}@[^\s@]{2,}\.[^\s@]{2,}$/.test(s);

  // 1. JWT decode (fast, works for work/org accounts)
  const payload = decodeJwtPayload(accessToken);
  if (payload) {
    const email = payload.preferred_username || payload.upn || payload.unique_name || payload.smtp;
    if (email && looksLikeEmail(email)) return email.toLowerCase();
  }

  // 2. Fetch most recent sent email and read the From address (reliable for personal accounts)
  try {
    const resp = await owaFetch("FindItem", {
      __type: "FindItemJsonRequest:#Exchange",
      Header: {
        __type: "JsonRequestHeaders:#Exchange",
        RequestServerVersion: "Exchange2016",
      },
      Body: {
        __type: "FindItemRequest:#Exchange",
        ItemShape: {
          __type: "ItemResponseShape:#Exchange",
          BaseShape: "IdOnly",
          AdditionalProperties: [
            { __type: "PropertyUri:#Exchange", FieldURI: "ItemLastModifiedTime" },
            { __type: "PropertyUri:#Exchange", FieldURI: "From" },
          ],
        },
        ParentFolderIds: [
          { __type: "DistinguishedFolderId:#Exchange", Id: "sentitems" },
        ],
        Traversal: "Shallow",
        Paging: {
          __type: "IndexedPageView:#Exchange",
          BasePoint: "Beginning",
          Offset: 0,
          MaxEntriesReturned: 1,
        },
        SortOrder: [
          {
            __type: "SortResults:#Exchange",
            Order: "Descending",
            Path: { __type: "PropertyUri:#Exchange", FieldURI: "DateTimeSent" },
          },
        ],
      },
    });
    const items = resp?.Body?.ResponseMessages?.Items?.[0]?.RootFolder?.Items;
    const fromEmail = items?.[0]?.From?.Mailbox?.EmailAddress;
    if (fromEmail && looksLikeEmail(fromEmail)) return fromEmail.toLowerCase();
  } catch (err) {
    if (DEBUG) console.warn("getOutlookEmail sent-item fetch failed:", err.message);
  }

  return null;
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

/** Convert HTML email body to plain text. */
function htmlToText(html) {
  if (!html) return "";
  let text = html;
  // Remove style/script/head blocks first (before any tag processing)
  text = text.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "");
  text = text.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "");
  text = text.replace(/<head[^>]*>[\s\S]*?<\/head>/gi, "");
  // Outlook-specific: remove <o:p> tags (empty paragraph spacers)
  text = text.replace(/<o:p>[\s\S]*?<\/o:p>/gi, "");
  // Line breaks
  text = text.replace(/<br\s*\/?>/gi, "\n");
  text = text.replace(/<hr\s*\/?>/gi, "\n---\n");
  // Block-level opening tags: insert newline before content
  text = text.replace(/<(?:p|div|blockquote|h[1-6])(?:\s[^>]*)?>(?!\s*<\/)/gi, "\n");
  // Block-level closing tags: insert newline after content
  text = text.replace(/<\/(?:p|div|blockquote|tr|li|h[1-6])>/gi, "\n");
  // List items: add bullet
  text = text.replace(/<li(?:\s[^>]*)?>/gi, "\n• ");
  // Table cells: tab-separate
  text = text.replace(/<\/(?:td|th)>/gi, "\t");
  // Strip remaining tags
  text = text.replace(/<[^>]+>/g, "");
  // Decode HTML entities
  text = text.replace(/&nbsp;/gi, " ");
  text = text.replace(/&amp;/gi, "&");
  text = text.replace(/&lt;/gi, "<");
  text = text.replace(/&gt;/gi, ">");
  text = text.replace(/&quot;/gi, '"');
  text = text.replace(/&#39;/gi, "'");
  text = text.replace(/&rsquo;/gi, "'");
  text = text.replace(/&lsquo;/gi, "'");
  text = text.replace(/&rdquo;/gi, "\u201D");
  text = text.replace(/&ldquo;/gi, "\u201C");
  text = text.replace(/&mdash;/gi, "\u2014");
  text = text.replace(/&ndash;/gi, "\u2013");
  text = text.replace(/&hellip;/gi, "\u2026");
  text = text.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(n));
  // Collapse whitespace (preserve newlines)
  text = text.replace(/[ \t]+/g, " ");
  text = text.replace(/ ?\n ?/g, "\n");
  text = text.replace(/\n{3,}/g, "\n\n");
  return text.trim();
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

  const rawHtml = item.Body?.Value || "";
  const bodyText = htmlToText(rawHtml);

  return {
    subject: item.Subject || "",
    sender: item.From?.Mailbox?.EmailAddress || "",
    sender_email: item.From?.Mailbox?.EmailAddress || "",
    sender_name: item.From?.Mailbox?.Name || "",
    body: bodyText,
    received_time: item.DateTimeReceived || "",
    has_attachments: item.HasAttachments || false,
    attachment_names: attachments.map((a) => a.Name).filter(Boolean),
    flag_status: item.Flag?.FlagStatus || "NotFlagged",
    conversation_id: item.ConversationId?.Id || null,
    conversation_topic: item.ConversationTopic || null,
    email_ref: item.ItemId?.Id || "",
    importance: IMPORTANCE_MAP[item.Importance] ?? 1,
    is_read: item.IsRead ?? true,
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

// --- GetItem batch — fetch multiple emails in one request -------------------

const GETITEM_BATCH_SIZE = 50; // OWA handles up to ~50-100 items per request

const GETITEM_PROPERTIES = [
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
  { __type: "PropertyUri:#Exchange", FieldURI: "IsRead" },
];

function buildGetItemBody(itemIds) {
  return {
    __type: "GetItemRequest:#Exchange",
    ItemShape: {
      __type: "ItemResponseShape:#Exchange",
      BaseShape: "IdOnly",
      AdditionalProperties: GETITEM_PROPERTIES,
      BodyType: "HTML",
    },
    ItemIds: itemIds,
  };
}

async function handleGetItemBatch(emails) {
  const itemIds = emails.map((e) => {
    const id = { __type: "ItemId:#Exchange", Id: e.email_ref };
    if (e._change_key) id.ChangeKey = e._change_key;
    return id;
  });

  const data = await owaFetch("GetItem", buildGetItemBody(itemIds));
  const items = data.Body?.ResponseMessages?.Items || [];

  const results = [];
  let fallbackCount = 0;
  // Iterate over emails.length — OWA may return fewer items than requested
  for (let i = 0; i < emails.length; i++) {
    const ri = items[i];
    if (ri?.ResponseCode === "NoError" && ri.Items?.[0]) {
      results.push(parseGetItemMessage(ri.Items[0]));
    } else {
      // Fall back to basic FindItem data for failed/missing items
      fallbackCount++;
      console.warn(`[GetItem] Fallback for "${emails[i].subject}"`,
        `| ResponseCode: ${ri?.ResponseCode || "MISSING"}`,
        `| Has Items: ${!!ri?.Items?.[0]}`);
      results.push(emails[i]);
    }
  }

  if (DEBUG && items.length !== emails.length) {
    console.warn(`GetItem returned ${items.length} responses for ${emails.length} requested items`);
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

  // Retry individual GetItem for emails that came back with empty bodies.
  // Batch GetItem can intermittently return empty Body.Value for some items.
  const emptyBodyIndices = [];
  for (let i = 0; i < enriched.length; i++) {
    if (!enriched[i].body && enriched[i].email_ref) {
      emptyBodyIndices.push(i);
    }
  }

  if (emptyBodyIndices.length > 0 && emptyBodyIndices.length < enriched.length) {
    if (DEBUG) console.log(`Retrying GetItem for ${emptyBodyIndices.length} email(s) with empty body`);
    for (const idx of emptyBodyIndices) {
      try {
        const email = enriched[idx];
        const itemId = { __type: "ItemId:#Exchange", Id: email.email_ref };
        if (email._change_key) itemId.ChangeKey = email._change_key;

        const data = await owaFetch("GetItem", buildGetItemBody([itemId]));
        const ri = data.Body?.ResponseMessages?.Items?.[0];
        if (ri?.ResponseCode === "NoError" && ri.Items?.[0]) {
          const retried = parseGetItemMessage(ri.Items[0]);
          if (retried.body) {
            enriched[idx] = retried;
            if (DEBUG) console.log(`  Retry succeeded for "${email.subject}"`);
          } else if (DEBUG) {
            console.warn(`  Retry still empty for "${email.subject}"`);
          }
        }
      } catch (err) {
        if (DEBUG) console.warn(`  Retry failed for index ${idx}:`, err.message);
      }
    }
  }

  const finalEmpty = enriched.filter((e) => !e.body).length;
  if (DEBUG && finalEmpty > 0) {
    console.warn(`After enrichment: ${finalEmpty}/${enriched.length} emails still have empty bodies`);
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

// --- DeleteItem — remove draft from Outlook --------------------------------

async function handleDeleteItem(itemId) {
  const body = {
    __type: "DeleteItemRequest:#Exchange",
    ItemIds: [
      { __type: "ItemId:#Exchange", Id: itemId },
    ],
    DeleteType: "HardDelete",
  };

  const data = await owaFetch("DeleteItem", body);
  const ri = data.Body?.ResponseMessages?.Items?.[0];

  if (ri?.ResponseCode === "NoError") {
    return { success: true };
  }

  // Draft already gone (user sent it or manually deleted it)
  if (ri?.ResponseCode === "ErrorItemNotFound") {
    return { success: true, already_gone: true };
  }

  throw new Error(`DeleteItem failed: ${ri?.ResponseCode || "unknown"}`);
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

async function discoverMailFolders() {
  // Return cache if still fresh
  if (cachedFolders && cachedFolders.length > 0 && folderCacheTime) {
    const age = Date.now() - new Date(folderCacheTime).getTime();
    if (age < FOLDER_CACHE_TTL_MS) return cachedFolders;
  }

  // OWA's service.svc does not support FindFolder. Instead, we inject a
  // script into the Outlook tab's MAIN world via chrome.scripting to read
  // folder IDs from React's internal fiber tree on the sidebar DOM nodes.
  if (!outlookTabId) throw new Error("No Outlook tab ID available");

  const results = await chrome.scripting.executeScript({
    target: { tabId: outlookTabId },
    world: "MAIN",
    func: () => {
      const SKIP = new Set([
        "favorites", "sent items", "drafts", "deleted items", "junk email",
        "outbox", "conversation history", "archive",
        "sync issues", "rss feeds", "rss subscriptions", "notes",
        "search folders",
      ]);
      const folderEls = document.querySelectorAll('[role="treeitem"][data-folder-name]');
      if (!folderEls.length) return null;
      const folders = [];
      const seen = new Set();
      folderEls.forEach(el => {
        const name = el.getAttribute("data-folder-name") || "";
        if (!name || SKIP.has(name.toLowerCase())) return;
        if (el.getAttribute("aria-level") === "1") return;
        const reactKey = Object.keys(el).find(k => k.startsWith("__reactFiber"));
        if (!reactKey) return;
        let current = el[reactKey];
        let folderId = null;
        let distinguishedFolderId = null;
        for (let i = 0; i < 8 && current; i++) {
          const props = current.memoizedProps || current.pendingProps;
          if (props && typeof props.folderId === "string" && props.folderId) {
            folderId = props.folderId;
            distinguishedFolderId = props.distinguishedFolderId || null;
            break;
          }
          current = current.return;
        }
        if (!folderId || seen.has(folderId)) return;
        seen.add(folderId);
        const title = el.getAttribute("title") || name;
        const displayName = title.split(" - ")[0].trim() || name;
        folders.push({ id: folderId, displayName, isDistinguished: !!distinguishedFolderId });
      });
      return folders.length > 0 ? folders : null;
    },
  });

  const folders = results?.[0]?.result;
  if (!folders || !folders.length) {
    throw new Error("No folders found in Outlook sidebar — Mail view may not be open");
  }

  cachedFolders = folders;
  folderCacheTime = new Date().toISOString();
  chrome.storage.local.set({ cachedFolders, folderCacheTime });

  if (DEBUG) console.log("Discovered mail folders:", folders.map(f => f.displayName));
  return folders;
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
    const profile = profiles?.[0];
    const existing = (profile?.user_email_aliases || []).map(a => a.toLowerCase());

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
    if (merged.length === existing.length && profile?.connected_outlook_email) return; // nothing new

    if (merged.length !== existing.length) {
      await patchProfileAliases(userId, merged);
      if (DEBUG) console.log("Updated user aliases:", merged);
    }

    // Lock Outlook account if not yet set — use the primary sent-from address
    if (!profile?.connected_outlook_email && detected.size > 0) {
      // Prefer the most common sent-from address; fall back to first detected
      const sentAddrs = sentEmails.map(e => (e.sender_email || "").toLowerCase()).filter(Boolean);
      const freq = {};
      for (const a of sentAddrs) freq[a] = (freq[a] || 0) + 1;
      const primary = Object.entries(freq).sort((a, b) => b[1] - a[1])[0]?.[0]
        || [...detected][0];
      try {
        await setConnectedOutlookEmail(userId, primary);
        if (DEBUG) console.log("Locked Outlook account via sent-items:", primary);
      } catch (err) {
        if (DEBUG) console.warn("Failed to lock Outlook email via alias:", err.message);
      }
    }
  } catch (err) {
    if (DEBUG) console.warn("Alias detection failed (non-blocking):", err.message);
  }
}

async function syncEmailsToSupabase() {
  if (DEBUG) console.log("syncEmailsToSupabase called");
  if (isSyncing) { if (DEBUG) console.log("Skipped — isSyncing is true"); return { skipped: true }; }
  isSyncing = true;

  try {
    // Ensure token is restored from storage (SW may have just woken up)
    await restoreToken();

    // Check both tokens exist
    if (!token || !token.token || isTokenExpired()) {
      if (DEBUG) console.log("Exiting — no valid Outlook token");
      return { error: "No valid Outlook token" };
    }

    const session = await getSupabaseSession();
    if (!session || !session.access_token) {
      if (DEBUG) console.log("Exiting — no Supabase session");
      return { error: "Not logged in to Supabase" };
    }

    const userId = session.user.id;
    if (DEBUG) console.log("userId:", userId);

    // --- Outlook account lock gate ---
    const outlookEmail = await getOutlookEmail(token.token);
    if (DEBUG) console.log("outlookEmail:", outlookEmail);
    if (outlookEmail) {
      // Read profile to check connected_outlook_email
      let profile;
      try {
        const profiles = await getProfile(userId);
        profile = profiles?.[0];
      } catch (err) {
        if (DEBUG) console.warn("getProfile failed:", err.message);
      }

      const connectedEmail = profile?.connected_outlook_email?.toLowerCase();

      if (!connectedEmail) {
        // First sync — lock this Outlook account
        try {
          if (DEBUG) console.log("Locking Outlook account:", outlookEmail);
          await setConnectedOutlookEmail(userId, outlookEmail);
        } catch (err) {
          console.warn("Failed to lock Outlook email:", err.message);
        }
      } else if (connectedEmail !== outlookEmail) {
        // Mismatch — abort sync
        console.warn(`Outlook mismatch: expected=${connectedEmail}, got=${outlookEmail}`);
        await chrome.storage.local.set({
          outlookMismatch: { expected: connectedEmail, actual: outlookEmail },
        });
        return { error: "OUTLOOK_MISMATCH", expected: connectedEmail, actual: outlookEmail };
      } else {
        // Match — clear any stale mismatch state
        if (DEBUG) console.log("Outlook email matches, proceeding");
        await chrome.storage.local.remove("outlookMismatch");
      }
    } else {
      if (DEBUG) console.log("No outlookEmail found, skipping lock gate");
      await chrome.storage.local.remove("outlookMismatch");
    }

    // Set limit: small for incremental syncs, larger for catch-up
    const maxEmails = lastSyncTime ? 50 : MAX_CATCHUP_EMAILS;

    // Discover mail folders from Outlook's sidebar DOM via chrome.scripting
    let folders;
    try {
      folders = await discoverMailFolders();
      if (DEBUG) console.log(`Discovered ${folders.length} folders:`, folders.map(f => f.displayName));
    } catch (err) {
      if (DEBUG) console.warn("Folder discovery failed, falling back to inbox-only:", err.message);
      folders = [{ id: null, displayName: "Inbox", isDistinguished: true }];
    }

    // Loop through each mail folder sequentially
    let totalSynced = 0;
    for (const folderInfo of folders) {
      try {
        if (DEBUG) console.log(`Fetching folder "${folderInfo.displayName}"`);
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
          is_read: e.is_read ?? true,
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
        console.error(`Error syncing folder "${folderInfo.displayName}":`, err.message);
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
          is_read: e.is_read ?? true,
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
  chrome.alarms.create(EMAIL_SYNC_ALARM, { delayInMinutes: EMAIL_SYNC_PERIOD_MIN });

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
    // Re-schedule immediately so the next sync is queued while this one runs
    chrome.alarms.create(EMAIL_SYNC_ALARM, { delayInMinutes: EMAIL_SYNC_PERIOD_MIN });

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

      // 3. Delete stale drafts (conversation already has a sent reply)
      if (session?.user?.id && token?.token && !isTokenExpired()) {
        try {
          await sweepStaleDrafts();
        } catch (err) {
          if (DEBUG) console.error("Alarm stale sweep error:", err.message);
        }
      }

      // 4. Sweep pending drafts missed while WebSocket was dead
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
    if (sender.tab?.id) outlookTabId = sender.tab.id;
    persistToken();
    updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "token_update" && !msg.data) {
    // Content script found no token
    if (!token || !token.token) updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "getStatus") {
    // Ensure token is restored from storage before reporting status
    // (service worker may have just woken up)
    restoreToken().then(() => {
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
    });
    return true; // keep message channel open for async response
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

  // Clear stale Outlook mismatch state on startup (email detection may have improved)
  await chrome.storage.local.remove("outlookMismatch");

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
