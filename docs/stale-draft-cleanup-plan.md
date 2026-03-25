# Stale Draft Cleanup — Implementation Plan

## Goal
When a user sends a reply in the same conversation as an AI-generated draft, hard-delete that draft from Outlook and mark it deleted in Supabase. Also prevent pending (not-yet-written) stale drafts from being pushed to Outlook in the first place.

## Detection Strategy
- Runs on the existing **2-minute alarm cycle**, as a new step inserted **before** `sweepPendingDrafts`
- A **Supabase RPC function** (`find_stale_drafts`) does the matching server-side:
  - Joins `drafts → emails (parent) → emails (sent in same conversation_id)`
  - Uses `auth.uid()` internally — no user ID parameter, no attack surface
  - Returns stale drafts in both `written` and `pending` states
- Extension calls the RPC, deletes from Outlook (written only), marks all as deleted in Supabase
- Capped at **10 deletes per cycle** to avoid OWA rate-limiting on backlog

## Changes

### 1. New migration: `supabase/migrations/020_draft_cleanup.sql`

Create `find_stale_drafts()` RPC function (`SECURITY INVOKER`, no parameters):
- Uses `auth.uid()` directly to scope to the calling user
- Returns `draft_id`, `outlook_draft_id`, `status` for drafts where:
  - `status IN ('written', 'pending')` and `draft_deleted = false`
  - A sent email exists in the same `conversation_id` (via `folder = 'Sent Items'`) with `received_time > d.created_at`
- Limited to 10 rows (`LIMIT 10`) to cap per-cycle work

No schema changes needed — `change_key` is not required for OWA `DeleteItem`.

### 2. `extension/background.js` — OWA delete handler + alarm rewiring

**Add `handleDeleteItem(itemId)`** after `handleSaveDraft` (~line 436):
- Builds `DeleteItemRequest` with `DeleteType: "HardDelete"`
- Calls `owaFetch("DeleteItem", body)`
- Treats `ErrorItemNotFound` as success (draft already gone)

**Reorder alarm handler** (line 740–768):
- Step 1: `syncEmailsToSupabase()` (unchanged)
- Step 2: Reconnect Realtime (unchanged)
- **Step 3: `sweepStaleDrafts()` (NEW — runs before pending sweep)**
- Step 4: `sweepPendingDrafts()` (moved after stale sweep)

Same guards as existing step 3: requires Supabase session + valid OWA token.

### 3. `extension/supabase-realtime.js` — stale draft sweep

**Add `sweepStaleDrafts()` function** after `sweepPendingDrafts`:
- Module-level `staleSweepInProgress` flag to prevent overlapping runs
- POST to `/rpc/find_stale_drafts` (empty body — function uses `auth.uid()`)
- For each result:
  - If `status === 'written'` and `outlook_draft_id` exists → call `handleDeleteItem(outlook_draft_id)`
  - If `status === 'pending'` → skip OWA call (nothing in Outlook yet)
  - PATCH draft: `{ draft_deleted: true, status: "deleted" }`
- Early exit on `TOKEN_EXPIRED`
- Telemetry: log `found / deleted / skipped` counts per cycle

### 4. UI filtering — exclude deleted drafts from queries

Deleted draft rows must not appear in any UI. Add `status != 'deleted'` filter at the **query level** in all three locations:

| File | Line | Current | Change |
|---|---|---|---|
| `extension/popup.js` | 113 | `drafts(id)` | `drafts(id,status)` + add `&drafts.status=neq.deleted` |
| `web/js/pages/dashboard.js` | 86 | `.select("...drafts(id, draft_body)")` | Add `.neq('drafts.status', 'deleted')` |
| `web/js/pages/emails.js` | 95, 101 | `.select("...drafts(*)")` | Add `.neq('drafts.status', 'deleted')` |

This filters at the PostgREST embed level — `hasDraft()` checks (`email.drafts.length > 0`) work unchanged since deleted drafts are excluded from the response.

## Error Handling

| Scenario | Behavior |
|---|---|
| Draft still in Outlook (`written`) | DeleteItem → mark `draft_deleted: true, status: "deleted"` |
| Stale pending draft (never written) | Skip OWA call → mark `draft_deleted: true, status: "deleted"` |
| User already sent/deleted the draft | `ErrorItemNotFound` → treat as success, still mark deleted |
| Token expired mid-sweep | Stop sweep, retry next alarm cycle |
| Network error | Log, skip draft, continue to next |
| Overlapping sweep (alarm + syncNow race) | `staleSweepInProgress` flag prevents double-fire |
| Large backlog (first deploy) | RPC returns max 10 rows; backlog drains over subsequent cycles |

## Files Modified

| File | Change |
|---|---|
| `supabase/migrations/020_draft_cleanup.sql` | **New.** `find_stale_drafts()` RPC |
| `extension/background.js` | `handleDeleteItem()` (~15 lines) + alarm reorder (~10 lines) |
| `extension/supabase-realtime.js` | `sweepStaleDrafts()` (~40 lines) |
| `extension/popup.js` | Query filter on drafts embed (line 113) |
| `web/js/pages/dashboard.js` | Query filter on drafts embed (line 86) |
| `web/js/pages/emails.js` | Query filter on drafts embed (lines 95, 101) |

## Known Limitations (v1)

- **Conversation-level matching:** Match is on `conversation_id`, not per-message `in_reply_to` (not stored in schema). Edge case: user replies to a different message in the same thread → draft for another message gets deleted. Low frequency under one-draft-per-conversation behavior.
- **No user-edit detection:** If the user is actively editing the AI draft in Outlook and separately sends a quick reply, the sweep hard-deletes their in-progress work. OWA's `LastModifiedTime` could detect this but requires an extra `GetItem` round-trip per draft. Deferred unless users report the issue.
- **2-minute delay:** Stale drafts linger until the next alarm cycle. Acceptable for v1; could be tightened by subscribing to `emails` INSERTs for sent items via Realtime.

## Verification

1. Apply migration: `supabase db push`
2. Load extension, trigger a draft generation for a test email
3. Confirm draft appears in Outlook Drafts folder and Supabase shows `status='written'`
4. Send a manual reply to the same conversation (not using the draft)
5. Wait for next alarm cycle (or trigger `syncNow`)
6. Confirm: draft removed from Outlook Drafts folder, Supabase row shows `draft_deleted=true, status='deleted'`
7. Verify popup, dashboard, and emails page no longer count the deleted draft
8. Test edge case: generate a draft, then immediately reply before the draft is written to Outlook — confirm the pending draft is marked deleted without ever hitting Outlook
