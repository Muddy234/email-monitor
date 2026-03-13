# Chrome Web Store Launch Plan

Pre-publication implementation plan for Clarion AI extension.

**Already complete:** Website (clarion-ai.app), privacy policy (privacy.html), store listing copy (chrome_store_listing.md), promo images, build script.

---

## Phase 1: Production Hardening (Required)

### 1.1 Set DEBUG flag to false + development artifact sweep

**File:** `extension/supabase-config.js` line 8

```javascript
// Change:
const DEBUG = true;
// To:
const DEBUG = false;
```

**Artifact sweep:** Before the final build, run a sweep for development remnants:

```powershell
# From project root — check for unguarded console.log, TODOs, test addresses, staging URLs
Select-String -Path extension\*.js -Pattern "console\.log|TODO|localhost|test@|staging" | Where-Object { $_ -notmatch "if \(DEBUG\)" }
```

**Current findings (9 console.log calls across 3 files):**

| File | Line | Guarded by DEBUG? | Action |
|------|------|-------------------|--------|
| `supabase-rest.js:62` | `console.log("Pushed ...")` | YES (`if (DEBUG && ...)`) | OK |
| `supabase-realtime.js:172` | `console.log("Realtime: draft written")` | YES | OK |
| `supabase-realtime.js:200` | `console.log("Realtime: connected")` | YES | OK |
| `supabase-realtime.js:225` | `console.log("Realtime: disconnected")` | YES | OK |
| `background.js:428` | `console.log("Enriched ...")` | **NO** | Wrap in `if (DEBUG)` |
| `background.js:556` | `console.log("Alias detection ...")` | YES | OK |
| `background.js:564` | `console.log("Updated user aliases ...")` | YES | OK |
| `background.js:633` | `console.log("Synced ... inbox emails")` | YES | OK |
| `background.js:674` | `console.log("Synced ... sent emails")` | YES | OK |

**Action:** Wrap `background.js:428` in a DEBUG guard. All others are already guarded and will go silent when DEBUG = false.

Also verify:
- No hardcoded test email addresses in popup.html or popup.js
- No staging/dev Supabase URLs (currently all point to production `frbvdoszenrrlswegsxq.supabase.co`)
- No leftover TODO comments visible in popup HTML

### 1.2 Branding consistency check

The extension is branded "Clarion AI" throughout. Verify no "email-monitor" remnants appear in user-facing surfaces:

**Current status:** No "email-monitor" strings found in `extension/` directory. However:
- Old zip in `dist/` may be named `email-monitor-1.0.0.zip` — delete it
- The project folder itself is named `Email_Monitor` — this is internal and doesn't affect the published extension
- The GitHub repo is `email-monitor` — consider renaming if the repo will be public, but not a store blocker

Manifest fields are correct:
- `name`: "Clarion AI"
- `short_name`: "Clarion"
- These match the store listing in `chrome_store_listing.md`

### 1.3 Add homepage_url to manifest

**File:** `extension/manifest.json`

Add after line 6 (`description`):

```json
"homepage_url": "https://clarion-ai.app",
```

Chrome Web Store uses this as the extension's website link.

---

## Phase 2: Permissions Justification Strategy

This is the #1 rejection reason on Chrome Web Store. Google's review team reads every permission justification and will reject with a generic "your extension requests permissions it doesn't appear to need" if justifications are vague or don't match observable behavior.

### 2.1 Current permissions inventory

**From manifest.json:**

```json
"permissions": ["alarms", "storage"],
"host_permissions": [
  "https://outlook.cloud.microsoft/*",
  "https://outlook.live.com/*",
  "https://outlook.office365.com/*",
  "https://outlook.office.com/*",
  "https://frbvdoszenrrlswegsxq.supabase.co/*"
]
```

**Content scripts (implicit host access):**
```json
"content_scripts": [{
  "matches": [
    "https://outlook.cloud.microsoft/*",
    "https://outlook.live.com/*",
    "https://outlook.office365.com/*",
    "https://outlook.office.com/*"
  ],
  "js": ["content.js"],
  "run_at": "document_idle"
}]
```

### 2.2 Single purpose description

Submit this in the Chrome Web Store "Single purpose" field:

> This extension syncs emails from Outlook Web to the Clarion AI service for AI-powered email analysis and draft reply generation. It captures the user's Outlook session token from localStorage to read inbox emails via the OWA API, pushes email metadata to Supabase for processing, and writes AI-generated draft replies back to the user's Outlook Drafts folder.

### 2.3 Per-permission justifications

These go in the "Permissions justification" field during Chrome Web Store submission. Each must be specific and reference observable behavior.

#### `alarms`

> Used to schedule a recurring 5-minute email sync cycle. The extension creates a single alarm ("email-sync") that triggers the service worker to check for new Outlook emails and push them to the Clarion AI backend. Without this permission, the extension cannot perform background syncing — the user would need to manually click "Sync Now" every time. No other alarms are created.

#### `storage`

> Used to persist three pieces of data across browser sessions via chrome.storage.local: (1) the user's Supabase authentication session (access token, refresh token, user ID) so they don't need to re-login after closing the browser, (2) the timestamp of the last successful email sync to avoid re-syncing already-processed emails, and (3) a single onboarding state key tracking whether the user has completed first-run setup. The MSAL/Outlook token is stored separately in chrome.storage.session (not synced, not persisted to disk — cleared when the browser closes).

#### Host permission: `https://outlook.cloud.microsoft/*`

> The service worker makes authenticated HTTP requests to Outlook's OWA service endpoint (outlook.cloud.microsoft/owa/service.svc) to execute three operations: FindItem (list inbox/sent emails), GetItem (fetch full email body and headers), and CreateItem (write AI-generated draft replies to the user's Drafts folder). These requests use the user's existing Outlook session token — the extension does not perform separate OAuth or store Microsoft credentials.

#### Host permission: `https://outlook.live.com/*`

> Same as outlook.cloud.microsoft — this is the OWA endpoint for personal Microsoft accounts (Outlook.com, Hotmail, Live). The service worker calls outlook.live.com/owa/0/service.svc for FindItem, GetItem, and CreateItem operations. Required to support users with personal Microsoft accounts in addition to work/school accounts.

#### Host permission: `https://outlook.office365.com/*`

> Same as outlook.cloud.microsoft — this is the OWA endpoint for Microsoft 365 business accounts. The service worker calls outlook.office365.com/owa/service.svc for the same three operations. Required to support users with Microsoft 365 subscriptions.

#### Host permission: `https://outlook.office.com/*`

> The content script (content.js) runs on outlook.office.com pages to capture the user's existing MSAL access token from localStorage. Specifically, it reads localStorage keys containing "accesstoken" and "outlook.office.com" where the token's target scope includes "mail.read". This token is sent to the service worker via chrome.runtime.sendMessage and used to authenticate OWA API requests. The content script does not modify the page DOM, inject UI elements, or intercept network requests — it only reads one localStorage entry.

#### Host permission: `https://frbvdoszenrrlswegsxq.supabase.co/*`

> The service worker makes authenticated REST API calls to our Supabase backend for four operations: (1) push email metadata (sender, subject, timestamps, body) for AI analysis, (2) read/update draft status when AI-generated drafts are delivered, (3) update the user's heartbeat timestamp so the backend knows the extension is active, and (4) maintain a WebSocket connection (Supabase Realtime) to receive new draft notifications in real-time. All requests use the user's Supabase JWT — no data is accessible without authentication.

### 2.4 Content script justification

Google may specifically ask about content scripts since they have page access. Prepare this response:

> content.js runs on Outlook Web pages (outlook.office.com, outlook.live.com, outlook.office365.com, outlook.cloud.microsoft) at document_idle. Its sole function is reading one MSAL access token entry from localStorage (the key containing "accesstoken" with "mail.read" scope). It does not modify the DOM, inject UI, intercept requests, or read any other localStorage entries. The token is sent to the service worker and used exclusively for OWA API calls to the user's own mailbox. The token is stored in chrome.storage.session (ephemeral, not synced across devices) and cleared when the browser session ends.

### 2.5 Reviewer FAQ preparation

Google reviewers sometimes ask follow-up questions. Have these ready:

**Q: Why do you need 4 separate Outlook host permissions?**
> Microsoft operates Outlook Web under 4 different domains depending on account type and region: outlook.office.com (web client), outlook.cloud.microsoft (new unified endpoint), outlook.live.com (personal accounts), outlook.office365.com (business accounts). Users may have their mailbox on any of these. We need host_permissions for all four to make OWA API calls and capture the MSAL token regardless of which domain the user accesses.

**Q: Do you send the Microsoft/MSAL token to your servers?**
> No. The MSAL token is stored only in chrome.storage.session (ephemeral, device-local) and used by the service worker to make direct API calls to Microsoft's OWA endpoints. The token is never included in any request to our Supabase backend. Our backend receives only email metadata (sender, subject, body, timestamps) — never authentication credentials.

**Q: Why not use Microsoft Graph API with OAuth instead of capturing tokens from localStorage?**
> We capture the user's existing OWA session token to avoid requiring a separate OAuth consent flow, which would add friction and require registering an Azure AD application. The user is already authenticated with Outlook Web — we leverage that existing session rather than asking them to authorize a second time. This approach accesses fewer scopes (only what the user already has) and doesn't require storing OAuth refresh tokens.

---

## Phase 3: User-Facing Error Messages

Current error messages are developer-oriented. Replace with actionable user guidance.

### 3.1 Badge tooltip context

**File:** `extension/background.js`

The badge shows three states (lines 61-80):
- Green checkmark = syncing normally
- Yellow "?" = no Outlook token
- Red "!" = error

Add `chrome.action.setTitle()` alongside each badge update so hovering the icon shows a human-readable tooltip:

| Badge state | Current tooltip | New tooltip |
|---|---|---|
| `ok` | (none) | "Clarion AI — syncing normally" |
| `no_token` | (none) | "Open Outlook Web to connect your email" |
| `error` | (none) | "Sync error — click for details" |

**Implementation:** In `updateBadge()`, add after each `setBadgeText` call:

```javascript
chrome.action.setTitle({ title: "Clarion AI — syncing normally" });
// or
chrome.action.setTitle({ title: "Open Outlook Web to connect your email" });
// or
chrome.action.setTitle({ title: "Sync error — click for details" });
```

### 3.2 Popup status view — token guidance

**File:** `extension/popup.js`, `refreshStatus()` function (lines 122-156)

When Outlook token is missing, the status view shows a yellow dot and "Not available." Replace with actionable text:

```
Current:  "Not available"
Replace:  "Open Outlook Web (outlook.office.com) in this browser to connect"
```

When token is expired:

```
Current:  "Expired"
Replace:  "Expired — refresh Outlook Web to reconnect"
```

### 3.3 Sync error display in popup

**File:** `extension/popup.js`, sync button handler (lines 253-264)

Currently sync errors show the raw error message. Wrap common errors with friendlier text:

| Raw error | User message |
|---|---|
| `"No valid Outlook token"` | "Please open Outlook Web first, then try again." |
| `"Not logged in to Supabase"` | "Please log in to Clarion AI first." |
| Network/fetch errors | "Connection error — check your internet and try again." |

### 3.4 Backend unreachable state

**File:** `extension/background.js`, `syncEmailsToSupabase()` function

Currently, if the Supabase backend is unreachable (Railway down, DNS failure, etc.), the sync fails silently with a generic error. Add a distinct state:

**Detection:** When `supabaseRequest()` throws and the error is a network-level failure (TypeError from fetch, or HTTP 502/503/504):

```javascript
// In syncEmailsToSupabase(), catch block:
if (err instanceof TypeError || /^Supabase (502|503|504)$/.test(err.message)) {
  return { error: "backend_unavailable" };
}
```

**User message in popup.js:** Map `"backend_unavailable"` to:
> "Clarion's servers are temporarily unavailable — we'll sync automatically when they're back."

**Badge:** Show red "!" badge with tooltip "Clarion servers unavailable — will retry automatically"

**Recovery:** The existing 5-minute alarm will retry automatically. No backoff needed for the MVP — the alarm fires every 5 minutes regardless, and a failing sync is cheap (one failed fetch).

---

## Phase 4: First-Run Onboarding

New users currently see a bare login form with no context. Add a welcome experience using a single state machine.

### 4.1 Onboarding state machine

**File:** `extension/popup.js`

Replace the planned `hasSeenWelcome` + `setupComplete` flags with a single `onboardingState` key in `chrome.storage.local`:

| State | Value | What user sees | Transitions to |
|---|---|---|---|
| Fresh install | `"welcome"` (or absent) | Welcome view | `"login"` on "Get Started" click |
| Seen welcome | `"login"` | Login form with context | `"setup"` on successful login |
| Logged in | `"setup"` | Setup checklist | `"complete"` when all 3 steps done |
| Ready | `"complete"` | Normal status view | (terminal) |

**Rendering logic in `checkSessionAndRender()`:**

```javascript
const { onboardingState } = await chrome.storage.local.get("onboardingState");
const state = onboardingState || "welcome";

if (state === "welcome") {
  showWelcomeView();
} else if (state === "login" || !session) {
  showLoginView();
} else if (state === "setup") {
  showSetupView(session);
} else {
  showStatusView(session);
}
```

**Edge case handling:**
- User clicks "Get Started" then closes popup → state is `"login"`, next open shows login form (correct)
- User logs in, never opens Outlook, reopens popup days later → state is `"setup"`, checklist shows step 2 incomplete (correct)
- User clears extension data → state resets to `"welcome"` (correct — re-onboard)
- User logs out → set state back to `"login"` (not `"welcome"` — they already know what the extension does)

### 4.2 Welcome view

**File:** `extension/popup.html`, `extension/popup.js`

Add `#welcomeView` div:

```
+----------------------------------+
|        [Clarion AI logo]         |
|                                  |
|  AI-powered email assistant      |
|  for Outlook                     |
|                                  |
|  1. Create an account            |
|  2. Open Outlook Web             |
|  3. Drafts appear automatically  |
|                                  |
|       [ Get Started ]            |
+----------------------------------+
```

"Get Started" sets `onboardingState = "login"` and calls `showLoginView()`.

### 4.3 Post-login setup checklist

**File:** `extension/popup.js`

After login, if `onboardingState === "setup"`, show a setup checklist instead of the raw status view:

```
+----------------------------------+
|  Welcome, {displayName}!         |
|                                  |
|  Setup checklist:                |
|  [x] Account created            |
|  [ ] Open Outlook Web            |
|      (outlook.office.com)        |
|  [ ] First sync complete         |
|                                  |
|  Waiting for Outlook...          |
+----------------------------------+
```

**Dynamic name:** Pull from `session.user.user_metadata.display_name` or fall back to the email prefix (everything before @).

**Checklist logic (checked on each 5-second refresh):**
- Step 1 complete: session exists (always true if we're in `"setup"` state)
- Step 2 complete: background returns `has_token: true` in getStatus response
- Step 3 complete: background returns `lastSyncTime` is set and response includes `synced > 0`

When all three complete, set `onboardingState = "complete"` and transition to normal status view.

### 4.4 Post-setup status view

After setup is complete, the existing status view works but should de-emphasize developer fields:

**Show prominently:**
- Last sync time + email count
- "Visit Dashboard" button
- Sync status (green/yellow/red indicator)

**Collapse:**
- Token expiry timestamp
- Token origin
- Supabase auth status

Implementation — wrap developer fields in a `<details>` element:

```html
<details>
  <summary>Advanced status</summary>
  <!-- token expiry, origin, auth status -->
</details>
```

---

## Phase 5: Store Screenshots

Chrome Web Store recommends 2-4 screenshots at 1280x800 or 640x400.

### Recommended screenshots

1. **Extension popup — setup complete**
   - Show the popup with green status, last sync time, email count
   - Overlay: "Always-on email monitoring"

2. **Dashboard — email prioritization**
   - Show the dashboard with a clear split between "needs response" and "safe to skip"
   - Overlay: "AI prioritizes what matters"

3. **Outlook — draft in action**
   - Show an AI-generated draft in Outlook's draft folder
   - Overlay: "AI-drafted replies ready for review"

4. **Dashboard — email list with classifications**
   - Show the main dashboard with email classifications and confidence indicators
   - Overlay: "Smart email analysis at a glance"

**Note:** Avoid showing the pipeline trace / scoring waterfall in store screenshots — it reads as developer tooling, not user benefit. Save that for a "How it works" section on clarion-ai.app where power users will appreciate the transparency.

### Screenshot capture process

1. Set up a demo account with representative data
2. Use browser at 1280x800 viewport
3. Capture with Windows Snipping Tool or browser DevTools device mode
4. Add text overlays in any image editor (Canva, Figma, etc.)
5. Save as PNG, place in `/promo/screenshots/`

Screenshots are uploaded separately to Chrome Web Store — they don't go in the extension zip.

---

## Phase 6: MSAL Token Security Verification

The extension intercepts bearer tokens from Outlook Web — sensitive credential material. Google's review team may specifically ask about this.

### 6.1 Token storage audit

| Storage location | What's stored | Synced across devices? | Persists after browser close? |
|---|---|---|---|
| `chrome.storage.session` (`exchangeToken`) | MSAL access token object | NO | NO (cleared on session end) |
| `chrome.storage.local` (`supabaseSession`) | Supabase JWT (access + refresh) | NO | YES |
| `chrome.storage.local` (`lastSyncTime`) | ISO timestamp string | NO | YES |
| `chrome.storage.local` (`onboardingState`) | State enum string | NO | YES |

**Verify:**
- [ ] MSAL token is NEVER stored in `chrome.storage.sync` (which syncs across devices via Google account)
- [ ] MSAL token is NEVER stored in `chrome.storage.local` (which persists to disk)
- [ ] Current implementation uses `chrome.storage.session` only — confirmed in background.js:89

### 6.2 Token transmission audit

The MSAL token should only be sent to Microsoft's OWA endpoints, never to Supabase or any other backend.

**Current flow:**
1. content.js reads token from localStorage → sends to service worker via `chrome.runtime.sendMessage`
2. Service worker stores in `chrome.storage.session` (ephemeral)
3. Service worker uses token in `Authorization: Bearer` header for OWA requests only (background.js:151)

**Verify:**
- [ ] `supabase-rest.js` never references `token.token` or `exchangeToken` — confirmed: it only uses `getValidAccessToken()` which returns the Supabase JWT, not the MSAL token
- [ ] `pushEmails()` body contains email metadata only (sender, subject, body, timestamps) — never the MSAL token
- [ ] `updateHeartbeat()` sends only timestamp and timezone — confirmed
- [ ] `updateDraftStatus()` sends only status and outlook_draft_id — confirmed
- [ ] No Supabase Realtime messages include the MSAL token

### 6.3 Token cleanup on logout

**Current behavior (popup.js:240-251):**
1. Calls `setWorkerActive(false)` to deactivate backend processing
2. Removes `supabaseSession` from `chrome.storage.local`
3. Sends `supabaseSessionChanged` message to background

**Missing:** Logout does not explicitly clear the MSAL token from `chrome.storage.session`. However, `chrome.storage.session` is ephemeral and auto-clears when the browser closes. Still, for defense in depth:

**Recommendation:** Add to logout handler:

```javascript
chrome.storage.session.remove("exchangeToken");
```

And in the background's `supabaseSessionChanged` handler, set `token = null`.

### 6.4 Token exposure summary for Google reviewers

If asked "How do you handle Microsoft auth tokens?":

> The MSAL access token is captured from Outlook Web's localStorage by the content script and stored exclusively in chrome.storage.session (ephemeral, not synced, cleared on browser close). It is used only for direct API calls to Microsoft's OWA endpoints (FindItem, GetItem, CreateItem) to read emails and write drafts. The token is never transmitted to our backend (Supabase), never persisted to disk, never synced across devices, and is explicitly cleared on logout. Our backend receives only email metadata — never Microsoft credentials.

---

## Phase 7: Pre-Submission Verification

### 7.1 Functional testing checklist

Test the full lifecycle on a clean Chrome profile:

- [ ] Install extension from zip (chrome://extensions -> Load unpacked)
- [ ] Welcome view appears on first popup open
- [ ] "Get Started" transitions to login view
- [ ] Sign up with new email -> confirmation email sent
- [ ] Confirm email -> log in -> setup checklist appears
- [ ] Checklist shows dynamic name (display name or email prefix, not hardcoded)
- [ ] Open outlook.office.com -> token captured -> checklist step 2 completes
- [ ] Wait for first sync (5 min or click "Sync Now") -> step 3 completes
- [ ] Normal status view shows after setup with developer fields collapsed
- [ ] Badge shows green checkmark with "syncing normally" tooltip
- [ ] "Visit Dashboard" opens clarion-ai.app/app/dashboard.html
- [ ] Logout -> login view appears (not welcome view), badge updates
- [ ] Close and reopen browser -> session persists, MSAL token cleared (session storage)
- [ ] Uninstall extension -> no errors, no orphaned data

### 7.2 Edge case testing

- [ ] Install with no internet -> graceful error on login attempt
- [ ] Login then lose internet -> badge shows error, recovers when reconnected
- [ ] Backend unreachable (Railway down) -> distinct "servers unavailable" message, auto-retry on next alarm
- [ ] Open Outlook in different browser -> token not captured, badge shows "?" with helpful tooltip
- [ ] Outlook token expires (wait 1h+) -> badge updates, re-opening Outlook refreshes token
- [ ] Sign up with existing email -> proper error message
- [ ] Empty inbox (0 emails) -> sync completes with count 0, no errors
- [ ] Very large inbox (1000+ emails) -> sync works within 5-min window

### 7.3 Security verification

**Supabase RLS:**
- [ ] RLS enabled on all tables (emails, drafts, classifications, contacts, threads, profiles, response_events, scoring_parameters, pipeline_runs, domains, user_topic_profile)
- [ ] Test: User A cannot read User B's emails via Supabase client with User A's JWT
- [ ] Anon key only allows authenticated operations (no public read/write without JWT)

**Extension security:**
- [ ] CSP blocks inline scripts (verify in popup.html — currently uses `script-src 'self'`)
- [ ] No secrets in source code beyond the public anon key
- [ ] MSAL token not in chrome.storage.sync (verified: uses chrome.storage.session only)
- [ ] MSAL token not included in any Supabase request body or headers
- [ ] MSAL token cleared on logout (add chrome.storage.session.remove if not already)
- [ ] No unguarded console.log calls leak sensitive data

**MSAL token path:**
- [ ] content.js reads only the one "accesstoken" + "mail.read" localStorage key
- [ ] Token sent only to service worker via chrome.runtime.sendMessage
- [ ] Service worker uses token only in Authorization header to OWA endpoints (background.js:151)
- [ ] Token never appears in pushEmails(), updateHeartbeat(), updateDraftStatus(), or Realtime messages

### 7.4 Backend health check

Before submitting to Chrome Web Store, confirm the backend is operational:

- [ ] Railway worker is running and processing emails
- [ ] Supabase is accessible from browser (test REST endpoint)
- [ ] Supabase Realtime WebSocket connects successfully
- [ ] Test a manual sync -> emails appear in Supabase -> classification runs -> draft created -> draft appears in Outlook
- [ ] clarion-ai.app is accessible, SSL cert valid
- [ ] Privacy policy page loads at https://clarion-ai.app/privacy.html

### 7.5 Chrome Web Store submission checklist

- [ ] Developer account created ($5 one-time fee at https://chrome.google.com/webstore/devconsole)
- [ ] Extension zip built and under 10MB
- [ ] All icon sizes present (16, 48, 128)
- [ ] Privacy policy URL accessible: https://clarion-ai.app/privacy.html
- [ ] Store listing text from chrome_store_listing.md entered
- [ ] Single purpose description entered (see Phase 2.2)
- [ ] Per-permission justifications entered (see Phase 2.3)
- [ ] Screenshots uploaded (2-4 at 1280x800)
- [ ] Promo images uploaded (440x280 small tile, 1400x560 marquee)
- [ ] Category: "Productivity" (consider "Communication" as alternative — email tools fit both, and Communication may have less competition)

---

## Implementation Order

| Step | Phase | Description |
|------|-------|-------------|
| 1 | 1.1 | Set DEBUG = false, wrap unguarded console.log, run artifact sweep |
| 2 | 1.2 | Verify branding consistency, delete old zips |
| 3 | 1.3 | Add homepage_url to manifest |
| 4 | 2 | Draft permission justifications (text only — no code changes) |
| 5 | 3.1-3.3 | Add badge tooltips |
| 6 | 3.2-3.3 | Improve token status messages + sync error wrapping |
| 7 | 3.4 | Add backend unreachable error state |
| 8 | 4.1 | Implement onboarding state machine |
| 9 | 4.2 | Add welcome view HTML/JS |
| 10 | 4.3 | Add setup checklist with dynamic name |
| 11 | 4.4 | Collapse developer fields in status view |
| 12 | 6.3 | Add explicit token cleanup on logout |
| 13 | — | **Rebuild extension zip** (immediately after last code change) |
| 14 | 7.1-7.4 | **Run full verification checklist on final build** |
| 15 | 5 | Capture store screenshots (parallel with steps 13-14) |
| 16 | 7.5 | Submit to Chrome Web Store |

---

## Files Modified

| File | Changes |
|------|---------|
| `extension/supabase-config.js` | DEBUG = false |
| `extension/manifest.json` | Add homepage_url |
| `extension/background.js` | Badge tooltips, backend unreachable detection, guard console.log on line 428, clear token on session change |
| `extension/popup.html` | Welcome view, setup checklist, details collapse for advanced status |
| `extension/popup.js` | Onboarding state machine, welcome/setup/status view logic, error message wrapping, token cleanup on logout, dynamic display name |

## Files Created

| File | Purpose |
|------|---------|
| `promo/screenshots/*.png` | Store listing screenshots (manual capture) |
