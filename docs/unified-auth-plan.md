# Unified Auth — Extension ↔ Website Session Sync

## Goal
Login to the Chrome extension or the website → automatically logged into both. Logout from either → logged out of both. Uses a content script on `clarion-ai.app` to bridge `chrome.storage.local` (extension) and `localStorage` (Supabase SDK).

## How It Works
- Extension stores session in `chrome.storage.local` under key `supabaseSession`
- Web app uses Supabase JS SDK v2 which stores session in `localStorage` under key `sb-frbvdoszenrrlswegsxq-auth-token`
- New content script `web-auth-sync.js` runs on `clarion-ai.app` pages and syncs between the two stores
- Both sides use the same Supabase project, so tokens are interchangeable

## Design Decisions
- **Content script over `externally_connectable`:** The content script approach keeps the website unaware of the extension's existence. `externally_connectable` would require the web codebase to include the extension ID and explicit `chrome.runtime.sendMessage` calls — coupling the web to the extension. Since the extension is optional, the content script is the better fit.
- **5s poll for web→extension sync:** The Supabase SDK writes to `localStorage` via same-tab `setItem`, which doesn't fire `StorageEvent`. Monkey-patching `localStorage.setItem` via injected page script is fragile across SDK updates. The 5s poll is pragmatic. **Tradeoff: up to 5-second delay on web→extension login propagation.** This is acceptable since login is not a time-critical hot path.
- **`chrome.storage.onChanged` for extension→web sync:** Fires immediately when the extension session changes — no polling needed in this direction.

## Files Modified

| File | Change |
|------|--------|
| `extension/manifest.json` | Add `clarion-ai.app` content script entry |
| `extension/web-auth-sync.js` | **New file** — bidirectional auth sync content script |
| `extension/popup.js` | Add `last_login_at` to session on login; ensure logout sends notification |
| `extension/background.js` | Relay `supabaseSessionChanged` to clarion-ai.app tabs |
| `web/js/auth.js` | Listen for `clarion:auth-revoked` CustomEvent → graceful redirect to login |

Note: No explicit `host_permissions` addition needed — `content_scripts.matches` implicitly grants host access. Adding it would be redundant and may trigger an extra permissions warning on extension update.

## 1. `extension/manifest.json` — add clarion-ai.app content script

Add to `content_scripts` array:
```json
{
  "matches": ["https://clarion-ai.app/*", "https://www.clarion-ai.app/*"],
  "js": ["web-auth-sync.js"],
  "run_at": "document_start"
}
```

## 2. `extension/web-auth-sync.js` — new content script

Responsibilities:
- **Extension → Web (on page load):** Read `chrome.storage.local` `supabaseSession`, convert to Supabase SDK format, write to `localStorage` if web has no session or a different user
- **Web → Extension (on auth change):** Poll `localStorage` every 5s for Supabase SDK session. If a new session appears (user logged in on web), convert and write to `chrome.storage.local`
- **Logout propagation:** If either store loses its session, clear the other. Dispatch `clarion:auth-revoked` CustomEvent instead of hard reload.

### Key format conversion

Extension format (`chrome.storage.local.supabaseSession`):
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1774709880,
  "last_login_at": 1774706280,
  "user": { "id": "...", "email": "...", "name": "..." }
}
```

Supabase SDK format (`localStorage['sb-frbvdoszenrrlswegsxq-auth-token']`):
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1774709880,
  "expires_in": 3600,
  "token_type": "bearer",
  "user": { "id": "...", "email": "...", ... }
}
```

Conversion is straightforward — the core fields (`access_token`, `refresh_token`, `expires_at`, `user`) overlap. The SDK format has extra fields (`token_type`, `expires_in`) populated with defaults.

When converting web→extension, add `last_login_at: Math.floor(Date.now() / 1000)` at the time of sync.

### Session validity check

A session is considered **valid** if it has a truthy `access_token`. This handles all logout scenarios:
- Key removed from storage (key absent)
- Key present but `access_token` is `null`, `""`, or `undefined`
- Key present but session object is empty/malformed

Helper used throughout:
```javascript
function isValidSession(session) {
  return session && session.access_token && session.user?.id;
}
```

### Logic flow

```
On page load:
  1. Async: chrome.storage.local.get("supabaseSession") → extSession
  2. AFTER step 1 resolves: sync read localStorage → webSession
     (reading localStorage after the async call prevents the race where
      Supabase SDK initializes and writes a session during the await)
  3. If extSession valid and webSession not valid → write extSession to localStorage
  4. If webSession valid and extSession not valid → write webSession to chrome.storage.local
  5. If both valid with different user IDs → newest wins (compare last_login_at)
  6. If neither valid → do nothing

Poll every 5s:
  - Detect new valid web session → push to chrome.storage.local (with last_login_at)
  - Detect web logout (key absent OR access_token falsy) → clear chrome.storage.local session

Listen chrome.storage.onChanged:
  - Extension session added/changed with valid access_token → push to localStorage
  - Extension session removed or invalidated → remove from localStorage
    + dispatch CustomEvent("clarion:auth-revoked") on document (no hard reload)
```

### Conflict resolution
When both sides have valid sessions for different users, the **newest session wins** based on `last_login_at` timestamp. This ensures a user who just logged in on the website as a different account isn't silently overwritten by a stale extension session.

### Reload loop prevention
Use `sessionStorage` flag `_clarion_auth_sync_reload` to prevent loops. Clear the flag after a successful sync completes so it doesn't block legitimate future syncs.

## 3. `extension/background.js` — relay session changes

Add handler for `supabaseSessionChanged` message type (popup already sends this on login). Forward to all `clarion-ai.app` tabs:

```javascript
if (msg.type === "supabaseSessionChanged") {
  chrome.tabs.query({ url: ["https://clarion-ai.app/*", "https://www.clarion-ai.app/*"] }, (tabs) => {
    for (const tab of tabs) {
      chrome.tabs.sendMessage(tab.id, { type: "ext_session_changed" }).catch(() => {});
    }
  });
}
```

## 4. `extension/popup.js` — session metadata + notifications

- Add `last_login_at: Math.floor(Date.now() / 1000)` to the session object when storing on login
- Ensure logout path sends `supabaseSessionChanged` to background (so content script on clarion-ai.app clears the web session)

## 5. `web/js/auth.js` — graceful logout on extension-initiated revocation

Add CustomEvent listener (~5 lines):
```javascript
window.addEventListener("clarion:auth-revoked", () => {
  window.location.replace("/app/login.html");
});
```

This replaces the hard page reload with a graceful redirect. User sees a clean transition to the login page instead of a jarring reload mid-task.

## Edge Cases

- **Extension not installed:** Web works normally — no content script injected, no CustomEvent dispatched, no interference
- **No clarion-ai.app tab open:** Extension session changes queue in `chrome.storage.local`, sync happens when user next visits the site
- **Token refresh:** Each side refreshes independently. Sync only handles login/logout, not every token refresh
- **Stale tokens:** Receiving side's refresh logic handles it (popup via `/auth/v1/token`, web SDK automatically)
- **Multiple clarion-ai.app tabs:** All receive `ext_session_changed` message; all update localStorage (idempotent)
- **Supabase SDK logout variants:** Handled by checking `isValidSession()` — covers `removeItem`, null session, empty access_token
- **Race condition on page load:** Eliminated by reading `localStorage` only after `chrome.storage.local.get` resolves

## Verification
1. Load updated extension in Chrome
2. **Extension → Web:** Log into extension popup → open `clarion-ai.app` → should be logged in automatically
3. **Web → Extension:** Log out of extension → log into `clarion-ai.app/app/login.html` → open extension popup → should be logged in (within ~5s)
4. **Logout from extension:** Log out of extension popup → `clarion-ai.app` tab should gracefully redirect to login (no hard reload)
5. **Logout from web:** Log out on website → open extension popup → should show login view
6. **Conflict resolution:** Log in on web as User A → log in on extension as User B → verify newest session wins on both sides
7. **No extension:** Open `clarion-ai.app` in a browser without the extension → normal login/logout works unchanged
