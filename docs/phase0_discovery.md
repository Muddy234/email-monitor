# Phase 0 Discovery — OWA service.svc Protocol

**Date:** 2026-03-04
**Environment:** Corporate (office365) — new Outlook (Monarch)

---

## Key Architectural Findings

1. **Domain changed:** Corporate Outlook now runs at `outlook.cloud.microsoft` (redirects from `outlook.office365.com`). Extension `host_permissions` must include `*://outlook.cloud.microsoft/*`.

2. **UI uses Hx protocol:** The new Outlook uses a proprietary sync protocol via a Service Worker (`/mail/0/sw.js`). Email list/body data does NOT flow through visible HTTP requests. However, `service.svc` is still functional for direct API calls.

3. **X-OWA-CANARY NOT required:** Bearer token alone is sufficient. All tested operations (FindItem, GetItem, UpdateItem, CreateItem, DeleteItem) worked with only `Authorization: Bearer {token}`.

4. **Cookies NOT required:** `credentials: 'omit'` returns `200 NoError`. The extension service worker can make calls without page cookies.

5. **Token source — MSAL cache in localStorage:**
   - Key pattern: `msal.2|{homeAccountId}|login.windows.net|accesstoken|{clientId}|{tenantId}|{scopes}||`
   - Filter: key contains `accesstoken` AND `outlook.office.com`, then check `target` contains `mail.read`
   - Token is in the `secret` field of the parsed JSON value
   - Token type: `Bearer`, JWT format, ~5172 chars
   - **Not** the `LokiAuthToken` in sessionStorage (that's for a different app)

6. **Token lifecycle:**
   - Lifetime: ~26 hours (`expiresOn - cachedAt`)
   - Extended lifetime: ~53 hours
   - MSAL refreshes automatically when the Outlook page is open
   - `lastUpdatedAt` field tracks refresh time

---

## Endpoint

```
POST https://outlook.cloud.microsoft/owa/service.svc?action={Action}&app=Mail
```

### Headers (all requests)

```
Content-Type: application/json
Authorization: Bearer {MSAL Exchange token}
Action: {Action}
```

### Request wrapper (all requests)

```json
{
  "__type": "{Action}JsonRequest:#Exchange",
  "Header": {
    "__type": "JsonRequestHeaders:#Exchange",
    "RequestServerVersion": "Exchange2013",
    "TimeZoneContext": {
      "__type": "TimeZoneContext:#Exchange",
      "TimeZoneDefinition": { "Id": "Central Standard Time" }
    }
  },
  "Body": { ... }
}
```

---

## FindItem — List messages in a folder

### Request Body

```json
{
  "__type": "FindItemRequest:#Exchange",
  "ItemShape": {
    "__type": "ItemResponseShape:#Exchange",
    "BaseShape": "IdOnly",
    "AdditionalProperties": [
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Subject" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "DateTimeReceived" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "DateTimeSent" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "From" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Importance" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "HasAttachments" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Flag" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "ConversationId" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "ConversationTopic" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "IsRead" }
    ]
  },
  "ParentFolderIds": [
    { "__type": "DistinguishedFolderId:#Exchange", "Id": "inbox" }
  ],
  "Traversal": "Shallow",
  "Paging": {
    "__type": "IndexedPageView:#Exchange",
    "BasePoint": "Beginning",
    "Offset": 0,
    "MaxEntriesReturned": 50
  }
}
```

**Notes:**
- `BaseShape` must be `"IdOnly"` — `"Default"` with AdditionalProperties causes `MemberAccessException` (500).
- `ToRecipients` and `CcRecipients` are NOT returned by FindItem even when requested. Use GetItem for those.
- Each `AdditionalProperties` entry MUST include `"__type": "PropertyUri:#Exchange"`.

### Response Structure

```
Body.ResponseMessages.Items[0]:
  ResponseCode: "NoError"
  ResponseClass: "Success"
  RootFolder:
    __type: "FindItemParentWrapper:#Exchange"
    TotalItemsInView: int
    IndexedPagingOffset: int
    IncludesLastItemInRange: bool
    Items: [...]
```

### Item Fields (per message)

| OWA Field | Type | Example |
|-----------|------|---------|
| `__type` | string | `"Message:#Exchange"` |
| `ItemId.Id` | string (68 chars) | EWS item identifier |
| `ItemId.ChangeKey` | string (40 chars) | Version key for updates |
| `Subject` | string | Email subject |
| `Importance` | string | `"Normal"`, `"High"`, `"Low"` |
| `ConversationTopic` | string | Thread topic |
| `From.Mailbox.Name` | string | Sender display name |
| `From.Mailbox.EmailAddress` | string | Sender email |
| `From.Mailbox.RoutingType` | string | `"SMTP"` |
| `From.Mailbox.MailboxType` | string | `"OneOff"` or `"Mailbox"` |
| `DateTimeReceived` | string | ISO 8601: `"2026-03-04T..."` |
| `DateTimeSent` | string | ISO 8601 |
| `HasAttachments` | bool | |
| `ConversationId.Id` | string (80 chars) | Conversation identifier |
| `Flag.FlagStatus` | string | `"NotFlagged"`, `"Flagged"`, `"Complete"` |
| `IsRead` | bool | |

---

## GetItem — Full message with body

### Request Body

```json
{
  "__type": "GetItemRequest:#Exchange",
  "ItemShape": {
    "__type": "ItemResponseShape:#Exchange",
    "BaseShape": "IdOnly",
    "AdditionalProperties": [
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Subject" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Body" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "From" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "ToRecipients" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "CcRecipients" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "DateTimeReceived" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Importance" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "HasAttachments" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Attachments" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "Flag" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "ConversationId" },
      { "__type": "PropertyUri:#Exchange", "FieldURI": "ConversationTopic" }
    ],
    "BodyType": "Text"
  },
  "ItemIds": [
    { "__type": "ItemId:#Exchange", "Id": "{itemId}", "ChangeKey": "{changeKey}" }
  ]
}
```

**Notes:**
- `BodyType: "Text"` returns plain text body. Use `"HTML"` for HTML.
- Response path: `Body.ResponseMessages.Items[0].Items[0]`

### Additional GetItem Fields

| OWA Field | Type | Structure |
|-----------|------|-----------|
| `Body.BodyType` | string | `"Text"` or `"HTML"` |
| `Body.Value` | string | Full message body |
| `Body.IsTruncated` | bool | |
| `ToRecipients` | array | `[{Name, EmailAddress, RoutingType, MailboxType, SipUri, Submitted, AADObjectId}]` |
| `CcRecipients` | array | Same structure as ToRecipients |
| `Attachments` | array | `[{__type, AttachmentId, Name, ContentType, ContentId, Size, LastModifiedTime, IsInline}]` |

Attachment `__type`: `"FileAttachment:#Exchange"`

---

## UpdateItem — Flag/unflag email

### Request Body

```json
{
  "__type": "UpdateItemRequest:#Exchange",
  "ItemChanges": [{
    "__type": "ItemChange:#Exchange",
    "ItemId": {
      "__type": "ItemId:#Exchange",
      "Id": "{itemId}",
      "ChangeKey": "{changeKey}"
    },
    "Updates": [{
      "__type": "SetItemField:#Exchange",
      "Item": {
        "__type": "Message:#Exchange",
        "Flag": {
          "__type": "FlagType:#Exchange",
          "FlagStatus": "NotFlagged"
        }
      },
      "Path": { "__type": "PropertyUri:#Exchange", "FieldURI": "Flag" }
    }]
  }],
  "ConflictResolution": "AlwaysOverwrite",
  "MessageDisposition": "SaveOnly"
}
```

**FlagStatus values:** `"Flagged"`, `"NotFlagged"`, `"Complete"`

### Response

```
Body.ResponseMessages.Items[0]:
  ResponseCode: "NoError"
  ConflictResults: {...}
  Items: [{ItemId: {Id, ChangeKey}}]   ← updated ChangeKey
```

---

## CreateItem — Save draft

### Request Body

```json
{
  "__type": "CreateItemRequest:#Exchange",
  "Items": [{
    "__type": "Message:#Exchange",
    "Subject": "RE: Original Subject",
    "Body": {
      "BodyType": "HTML",
      "Value": "<html><body><p>Draft content here.</p></body></html>"
    },
    "ToRecipients": [{
      "Name": "Recipient Name",
      "EmailAddress": "recipient@example.com",
      "RoutingType": "SMTP"
    }]
  }],
  "MessageDisposition": "SaveOnly"
}
```

**Critical notes:**
- Do NOT use `__type` annotations on `Body` or `ToRecipients` child objects — causes `OwaSerializationException` (500).
- Do NOT include `SavedItemFolderId` — also causes serialization error. Drafts folder is the default.
- `MessageDisposition: "SaveOnly"` saves as draft without sending.
- HTML and Text body types both work.

### Response

```
Body.ResponseMessages.Items[0]:
  ResponseCode: "NoError"
  Items: [{__type, ItemId: {Id, ChangeKey}}]
```

---

## DeleteItem — Remove draft (cleanup)

### Request Body

```json
{
  "__type": "DeleteItemRequest:#Exchange",
  "ItemIds": [
    { "__type": "ItemId:#Exchange", "Id": "{itemId}", "ChangeKey": "{changeKey}" }
  ],
  "DeleteType": "MoveToDeletedItems"
}
```

**DeleteType values:** `"MoveToDeletedItems"`, `"SoftDelete"`, `"HardDelete"`

---

## OWA → email_data Field Mapping

| email_data key | OWA source (FindItem) | OWA source (GetItem) | Transform |
|----------------|----------------------|---------------------|-----------|
| `subject` | `Subject` | `Subject` | direct |
| `sender` | `From.Mailbox.EmailAddress` | same | direct |
| `sender_email` | `From.Mailbox.EmailAddress` | same | direct |
| `sender_name` | `From.Mailbox.Name` | same | direct |
| `body` | — | `Body.Value` | direct |
| `received_time` | `DateTimeReceived` | same | ISO string |
| `has_attachments` | `HasAttachments` | same | direct (bool) |
| `attachment_names` | — | `Attachments[].Name` | map to list |
| `folder` | (from request ParentFolderIds) | — | e.g. `"Inbox"` |
| `flag_status` | `Flag.FlagStatus` | same | string: `"Flagged"` / `"NotFlagged"` |
| `conversation_id` | `ConversationId.Id` | same | direct |
| `conversation_topic` | `ConversationTopic` | same | direct |
| `email_ref` | `ItemId.Id` | same | direct |
| `to_field` | — | `ToRecipients[].EmailAddress` | join with `"; "` |
| `cc_field` | — | `CcRecipients[].EmailAddress` | join with `"; "` |
| `importance` | `Importance` | same | map: `"Low"→0, "Normal"→1, "High"→2` |
| `recipients` | — | ToRecipients + CcRecipients | `[{name, address, type}]` where type: To=1, CC=2 |

---

## Token Capture Strategy for Extension

### Content script approach (primary)

```javascript
// Content script on outlook.cloud.microsoft pages
function findExchangeToken() {
  const keys = Object.keys(localStorage);
  for (const key of keys) {
    if (!key.includes('accesstoken') || !key.includes('outlook.office.com')) continue;
    try {
      const entry = JSON.parse(localStorage.getItem(key));
      if (entry.target && entry.target.toLowerCase().includes('mail.read')) {
        return {
          token: entry.secret,
          expiresOn: parseInt(entry.expiresOn),
          cachedAt: parseInt(entry.cachedAt),
          clientId: entry.clientId
        };
      }
    } catch (e) { continue; }
  }
  return null;
}
```

### Token refresh
- MSAL refreshes tokens automatically when Outlook page is open
- Content script polls localStorage periodically (e.g. every 60s) and sends updated token to service worker
- If token expired and no refresh available, prompt user to interact with Outlook tab

### webRequest approach (secondary/validation)
- `chrome.webRequest.onBeforeSendHeaders` on `service.svc` URLs can capture `Authorization` header from OWA's own requests
- Useful as validation that token is current, but localStorage is more reliable for initial capture since OWA uses Hx protocol (not service.svc) for most operations

---

## Extension manifest.json Updates Needed

```json
{
  "host_permissions": [
    "*://outlook.cloud.microsoft/*",
    "*://outlook.live.com/*",
    "*://outlook.office365.com/*",
    "*://outlook.office.com/*",
    "http://localhost/*"
  ],
  "content_scripts": [{
    "matches": [
      "https://outlook.cloud.microsoft/*",
      "https://outlook.live.com/*",
      "https://outlook.office365.com/*"
    ]
  }]
}
```

---

## Summary of Tested Operations

| Operation | Action | Status | Auth | Cookies |
|-----------|--------|--------|------|---------|
| List emails | FindItem | ✅ 200 | Bearer only | Not needed |
| Get full email | GetItem | ✅ 200 | Bearer only | Not needed |
| Flag email | UpdateItem (Flagged) | ✅ 200 | Bearer only | Not needed |
| Unflag email | UpdateItem (NotFlagged) | ✅ 200 | Bearer only | Not needed |
| Create draft | CreateItem (SaveOnly) | ✅ 200 | Bearer only | Not needed |
| Delete item | DeleteItem | ✅ 200 | Bearer only | Not needed |
