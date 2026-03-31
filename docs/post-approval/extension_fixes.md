# Extension Fixes — Post Chrome Web Store Approval

Queued changes that require an extension update. Batch these into the first post-approval release to minimize resubmissions.

## Summary

- [ ] **#1** Use `getValidAccessToken()` in sync loop — needs fix
- [ ] **#2** Detect auth errors in `supabaseRequest()` — needs fix
- [x] **#3** Remove `onboarding_completed_at` gate from sync signal — fixed in `610ec2e`
- [ ] **#4** Reload Outlook tab on MSAL token expiry — needs fix
- [x] **#5** Fix pre-onboarding email cap dropping after first cycle — fixed

---

## 1. Use `getValidAccessToken()` in sync loop

**File:** `extension/background.js` (line ~978)

**Problem:** `syncEmailsToSupabase()` calls `getSupabaseSession()`, which reads the cached session from `chrome.storage.local` without refreshing or validating. If the session is stale (e.g. user deleted account, token expired server-side), the sync loop runs with a dead token indefinitely.

**Fix:** Replace `getSupabaseSession()` with `getValidAccessToken()` so the token is refreshed when near expiry. Extract the user ID from the JWT or fetch it after refresh.

---

## 2. Detect auth errors in `supabaseRequest()` and clear stale session

**File:** `extension/supabase-rest.js`

**Problem:** `supabaseRequest()` is the central HTTP wrapper for all Supabase calls (`pushEmails`, `getProfile`, `setInitialSyncComplete`, etc.). It does not check for 401/403 responses. Auth failures are caught by per-folder try/catch blocks in `background.js` and silently swallowed — the sync appears to succeed with 0 emails landing.

**Fix:** In `supabaseRequest()`, check for 401/403 status. On auth failure:
- Clear `supabaseSession` from `chrome.storage.local`
- Set an error badge (e.g. red dot + "Login required")
- Throw a typed error (e.g. `AuthError`) so callers can short-circuit the sync loop instead of retrying every folder

---

## 3. Remove `onboarding_completed_at` gate from initial sync signal

**File:** `extension/background.js` (line ~1202)

**Status:** Fixed in `610ec2e` — verify this commit is included in the submitted build. If not, include in post-approval update.

**Problem:** The `initial_sync_complete` signal required `!profile.onboarding_completed_at`, which permanently blocked the flag from being set after an onboarding re-run.

---

## 4. Reload Outlook tab when MSAL token is expired

**Files:** `extension/content.js`, `extension/background.js`

**Problem:** The content script reads the MSAL Exchange token from Outlook's `localStorage` every 60s. When the token expires, Outlook's in-page MSAL client silently refreshes it in memory, but the updated token may not be written back to `localStorage` in a format the content script can read. The extension continues using the stale token until the user manually logs out and back in.

**Fix:** In `content.js`, check `expiresOn` before sending the token. If expired, send a `token_expired` message instead. In `background.js`, on receiving `token_expired`, call `chrome.tabs.reload(outlookTabId)` to force Outlook to re-initialize MSAL — which acquires a fresh token and writes it to `localStorage`. The content script picks up the new token on page load. Gate the reload to at most once per expiry cycle to avoid reload loops.

---

## 5. Fix pre-onboarding email cap dropping after first cycle

**File:** `extension/background.js`

**Status:** Fixed.

**Problem:** `isInitialSync` was gated on `!hasCompletedFolderSync`, which flips to `true` after the first folder discovery. The 200/folder cap dropped to 50/folder on the second cycle. Combined with OWA `FindItem` only returning the N most recent emails (deduped via upsert), if the first burst didn't clear 500, onboarding never triggered.

**Fix:** Three changes:
1. Removed `hasCompletedFolderSync` from cap logic — cap now depends solely on `onboarding_completed_at`
2. Moved folder discovery before set-email-cap (discover folders first, then decide how many to fetch)
3. Simplified to two caps: `PRE_ONBOARDING_EMAIL_CAP = 500` / `POST_ONBOARDING_EMAIL_CAP = 50` — applied to both inbox and sent items
4. Removed force-catchup-mode hack (no longer needed with simplified cap)
5. Removed unused `MAX_CATCHUP_EMAILS = 1000` constant

---
