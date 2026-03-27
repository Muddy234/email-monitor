# Fix: Drafts written to wrong Outlook account

## Problem
When two Outlook accounts are open in the same browser, `content.js` picks up the **work account's** `mail.read` token (higher priority) and silently replaces the personal account's token in the service worker. Sync correctly detects the mismatch and aborts, but **draft writing has no such guard** — drafts get written to the wrong mailbox.

## Approach: Two-layer defense

### Layer 1 — Reject wrong-account tokens at the source (fail open)
Gate the `token_update` handler in `background.js` so tokens from a mismatched account are never cached. Fails open on errors — don't brick the extension if profile fetch fails.

### Layer 2 — Guard draft operations before OWA calls (fail closed)
Check before any OWA write operation. **Fail closed** — if we can't verify account match, don't write the draft. A delayed draft is cheaper than a draft in the wrong mailbox. Pending drafts retry on the next sweep cycle (45s).

## Changes Made

### `extension/background.js`

1. **Module-level cache**: `let connectedOutlookEmail = null;` — populated on startup, during sync, and cleared on logout.

2. **`getTokenEmail(rawToken)`** — Synchronous JWT-only email extraction for Layer 1 (fast, best-effort).

3. **`isTokenAccountMismatch()`** — Async guard using `getOutlookEmail()` (JWT + sent-item fallback) for Layer 2. Fails closed when verification is impossible, passes through only when no lock is established (first sync).

4. **`token_update` handler gated** — Rejects tokens from wrong account, sets `outlookMismatch` in storage (triggers popup banner), sets mismatch badge.

5. **Mismatch badge state** — Orange `!` with "Wrong Outlook account, drafts paused" tooltip.

6. **Startup cache population** — Fetches profile in startup IIFE before alarms/realtime fire (closes SW restart race window).

7. **Sync piggyback** — Updates `connectedOutlookEmail` from existing profile fetch in `syncEmailsToSupabase()`.

8. **Logout cleanup** — Clears `connectedOutlookEmail` in `supabaseSessionChanged` handler.

### `extension/supabase-realtime.js`

9. **`handleNewDraft()` guarded** — Early return if `isTokenAccountMismatch()`. Draft stays pending.

10. **`sweepPendingDrafts()` guarded** — Skip sweep if token mismatch. Drafts retry next cycle.

11. **`sweepStaleDrafts()` intentionally unguarded** — Stale sweep with wrong token can clean up drafts previously written to the wrong mailbox. Guarding here would leave orphans.

## Edge Cases

| Scenario | Layer 1 (token_update) | Layer 2 (draft guards) |
|----------|----------------------|----------------------|
| JWT decodes, mismatch | Reject token | Skip draft |
| JWT decodes, match | Accept token | Write draft |
| Opaque token (decode fails) | Accept (fail open) | `getOutlookEmail()` sent-item fallback verifies correctly |
| No lock yet (first sync) | Accept token | Pass through |
| Profile fetch fails | Accept (fail open) | Fail closed |
| SW restart, token before profile | N/A | `isTokenAccountMismatch()` fetches profile inline |
| User reconnects different account | Cache cleared on logout | Fresh state |

## Verification
1. Load extension with two Outlook accounts open
2. Log into extension with personal account, confirm `connected_outlook_email` is set
3. Observe work token rejected → console logs `Rejected token from ...`
4. Verify badge shows orange `!` with "Wrong Outlook account, drafts paused"
5. Trigger a draft → confirm it stays `pending`, does NOT appear in work account
6. **Recovery**: Close work tab → wait for poll (60s) → correct token captured → badge clears → next sweep (45s) delivers pending draft to correct account
7. Popup mismatch banner shows/clears correctly
8. **Single personal account**: Verify drafts still write normally (sent-item fallback handles opaque tokens)
