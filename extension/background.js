/**
 * Service worker (background) — Email Monitor Outlook Bridge.
 *
 * Responsibilities:
 *  1. Receive + cache MSAL Exchange token from content script.
 *  2. Maintain WebSocket connection to Flask backend.
 *  3. Execute OWA service.svc commands on behalf of Flask.
 *  4. Alarm-driven reconnect for MV3 idle timeout.
 */

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const WS_URL = "ws://localhost:5000/extension";
const ALARM_NAME = "ws-reconnect";
const ALARM_PERIOD_MIN = 0.42; // ~25 s
const MAX_BACKOFF_MS = 5 * 60 * 1000; // 5 min
const BASE_BACKOFF_MS = 25_000;

// OWA endpoint templates
const OWA_ENDPOINTS = {
  "outlook.cloud.microsoft": "/owa/service.svc",
  "outlook.live.com": "/owa/0/service.svc",
  "outlook.office365.com": "/owa/service.svc",
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let ws = null;
let token = null;        // { token, expiresOn, cachedAt, clientId, origin }
let backoffMs = 0;
let lastCommand = null;  // { action, timestamp }

// ---------------------------------------------------------------------------
// Badge helpers
// ---------------------------------------------------------------------------

function setBadge(status) {
  const map = {
    connected: { text: "", color: "#22c55e" },      // green
    disconnected: { text: "!", color: "#ef4444" },   // red
    no_token: { text: "?", color: "#eab308" },       // yellow
  };
  const cfg = map[status] || map.disconnected;
  chrome.action.setBadgeText({ text: cfg.text });
  chrome.action.setBadgeBackgroundColor({ color: cfg.color });
}

function updateBadge() {
  if (!token || !token.token) {
    setBadge("no_token");
  } else if (!ws || ws.readyState !== WebSocket.OPEN) {
    setBadge("disconnected");
  } else {
    setBadge("connected");
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
    ws_connected: ws && ws.readyState === WebSocket.OPEN,
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
// WebSocket connection
// ---------------------------------------------------------------------------

function connectWebSocket() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  try {
    ws = new WebSocket(WS_URL);
  } catch (_) {
    ws = null;
    updateBadge();
    return;
  }

  ws.onopen = () => {
    backoffMs = 0;
    updateBadge();
    // Announce ourselves with token status
    ws.send(JSON.stringify({
      type: "extension_hello",
      has_token: !!(token && token.token),
      token_expired: isTokenExpired(),
    }));
  };

  ws.onmessage = async (event) => {
    let command;
    try {
      command = JSON.parse(event.data);
    } catch (_) {
      return;
    }

    const response = await dispatchCommand(command);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(response));
    }
  };

  ws.onclose = () => {
    ws = null;
    updateBadge();
  };

  ws.onerror = () => {
    ws = null;
    updateBadge();
  };
}

// ---------------------------------------------------------------------------
// Alarm-driven reconnect with exponential backoff
// ---------------------------------------------------------------------------

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== ALARM_NAME) return;

  if (ws && ws.readyState === WebSocket.OPEN) {
    return; // alive, nothing to do
  }

  // Backoff check
  if (backoffMs > 0) {
    // Decrease backoff by alarm interval and skip if still waiting
    backoffMs = Math.max(0, backoffMs - ALARM_PERIOD_MIN * 60 * 1000);
    if (backoffMs > 0) return;
  }

  connectWebSocket();

  // If connection fails immediately, increase backoff
  setTimeout(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      backoffMs = Math.min(
        (backoffMs || BASE_BACKOFF_MS) * 2,
        MAX_BACKOFF_MS
      );
    }
  }, 3000);
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
      ws_connected: ws && ws.readyState === WebSocket.OPEN,
      last_command: lastCommand,
      backoff_ms: backoffMs,
    });
  }
  return false; // synchronous response
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

(async () => {
  await restoreToken();
  updateBadge();

  // Start alarm for WS reconnect
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MIN });

  // Attempt initial WS connection
  connectWebSocket();
})();
