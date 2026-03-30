# Plan: Fix Subfolder Sync, Gate Onboarding on Sync Completion, Fix Email Selection

## Summary

Ten coordinated changes across extension, worker, and database:

1. **Extension** — Expand folder tree before DOM discovery so collapsed subfolders are found
2. **Extension** — Track folder discovery success; set `initial_sync_complete` on profile after first successful multi-folder sync
3. **Extension** — Use larger per-folder email cap during initial sync to reach 500 threshold faster
4. **Worker** — Gate onboarding on `initial_sync_complete = true` + `email_count >= 500` + remove premature fallback trigger
5. **Worker** — Require minimum sent email count before building style/behavior guides
6. **Worker** — Over-fetch emails (1500), pre-filter spam/noise, then cap at 500 most recent clean emails
7. **Worker** — Add minimum extracted-feature threshold before Sonnet synthesis
8. **Worker** — Track extracted feature count (not sampled count) in DB
9. **Worker** — Add sample-size awareness to Sonnet synthesis prompts
10. **Database** — New migration adding `initial_sync_complete` boolean column

---

## Change 1: New Migration

**New file:** `supabase/migrations/030_initial_sync_complete.sql`

```sql
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS initial_sync_complete boolean DEFAULT false;

-- Rename to reflect new semantics: now tracks extracted features, not sampled emails (Change 9)
ALTER TABLE public.profiles
  RENAME COLUMN style_sample_count TO style_extracted_feature_count;
```

- `initial_sync_complete` defaults `false` — existing users without onboarding are gated until extension sets it
- Already-onboarded users are unaffected (worker skips them via `onboarding_completed_at IS NULL` check)
- Column rename makes the semantic change explicit in the schema rather than a silent documentation-only shift

---

## Change 2: Extension — Expand Collapsed Folders Before DOM Discovery

**File:** `extension/background.js`

### Problem

`discoverMailFolders()` (line 781) reads sidebar DOM nodes via `[role="treeitem"][data-folder-name]`. Collapsed parent folders don't render their children in the DOM — only expanded/visible tree items are discoverable. This means nested subfolders (e.g., Inbox > Becky > Projects) are invisible unless the user manually expanded them.

OWA's `service.svc` does NOT support `FindFolder` (confirmed by comment at line 788), so an API-based approach is not viable. The fix must work within the DOM-based constraint.

### Solution

**2a.** Inside the injected `chrome.scripting.executeScript` function in `discoverMailFolders()` (line ~796), before the folder enumeration loop, guard against colliding with active user interaction, then expand collapsed tree items:

```js
// Skip expand/collapse if user is actively interacting with the tab —
// programmatic collapse could fight with their clicks on the folder tree.
// Falls back to visible-only discovery (same as today's behavior).
if (document.hasFocus()) {
  // proceed to folder enumeration without expanding
} else {
```

Then, inside the `else` block, add the expansion logic:

```js
// Expand collapsed folder tree items to reveal nested subfolders
const expandable = document.querySelectorAll(
  '[role="treeitem"][data-folder-name][aria-expanded="false"]'
);
for (const el of expandable) {
  const name = (el.getAttribute("data-folder-name") || "").toLowerCase();
  // Only expand mail folders under Inbox, skip system folders
  const SKIP_EXPAND = new Set([
    "favorites", "sent items", "drafts", "deleted items", "junk email",
    "outbox", "conversation history", "archive",
    "sync issues", "rss feeds", "rss subscriptions", "notes",
    "search folders",
  ]);
  if (SKIP_EXPAND.has(name)) continue;
  el.querySelector('[role="button"], .ms-Button, [data-icon-name="ChevronRight"]')?.click();
}
```

**2b.** After expanding, poll for new treeitem elements rather than using a fixed delay:

```js
// Wait for DOM to render children of expanded folders
const countBefore = document.querySelectorAll('[role="treeitem"][data-folder-name]').length;
const POLL_INTERVAL_MS = 100;
const POLL_TIMEOUT_MS = 2000;
let elapsed = 0;
while (elapsed < POLL_TIMEOUT_MS) {
  await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
  elapsed += POLL_INTERVAL_MS;
  const countNow = document.querySelectorAll('[role="treeitem"][data-folder-name]').length;
  if (countNow > countBefore) break;  // new children rendered
}
```

Note: `chrome.scripting.executeScript` supports async functions. The injected function should be declared `async`.

**Why polling over fixed delay:** A fixed 500ms wait is fragile — fast machines wait unnecessarily, slow machines or large folder trees under load may not finish rendering. Polling every 100ms with a 2s ceiling adapts to the actual render speed. If no new elements appear within 2s, we proceed with whatever was visible (same as today's fallback).

**2c.** After the folder enumeration completes (still inside the `else` block from 2a), collapse the folders back to avoid disrupting the user's sidebar state:

```js
// Restore collapsed state
for (const el of expandable) {
  if (el.getAttribute("aria-expanded") === "true") {
    el.querySelector('[role="button"], .ms-Button, [data-icon-name="ChevronDown"]')?.click();
  }
}
} // close the else block from 2a
```

### Risks & Mitigations

- **Chevron selector fragility** — OWA may change button markup. The selector chain (`[role="button"]`, `.ms-Button`, `[data-icon-name]`) provides fallbacks. If none match, expansion silently fails and we fall back to current behavior (visible folders only).
- **User interaction collision** — The `document.hasFocus()` guard (2a) skips the expand/collapse pass entirely when the Outlook tab is focused. If the user is actively browsing their folder tree, discovery falls back to visible-only folders (same as today). Subfolders will be discovered on a subsequent cycle when the tab loses focus.
- **Deep nesting** — This expands one level. For deeper nesting (3+ levels), we'd need to loop expansion until no new `aria-expanded="false"` items appear. Start with single-pass; iterate if users report missing deep folders.

---

## Change 3: Extension — Signal Sync Completion

**Files:** `extension/background.js`, `extension/supabase-rest.js`

### background.js

**3a.** Add module-level flag (near line 44, by `cachedFolders`):
```js
let hasCompletedFolderSync = false;
```

**3b.** Seed the in-memory flag from profile on each sync cycle. `getProfile()` already runs every cycle (line 956). After line 957 (`profile = profiles?.[0]`), add:
```js
if (profile?.initial_sync_complete) hasCompletedFolderSync = true;
```
This handles MV3 service worker kills — on re-wake, the flag is restored from the DB rather than requiring another discovery round.

**3c.** Set flag when folder discovery succeeds (line ~1003, inside `try` block):
- After `const discovered = await discoverMailFolders();` — set `hasCompletedFolderSync = true`
- This means the sidebar was readable. Even if 0 subfolders exist, discovery worked.

**3d.** Fix infinite catchup loop (lines 990-994). Replace with:
```js
if (profile && !profile.onboarding_completed_at && lastSyncTime && !hasCompletedFolderSync) {
  const syncAgeMs = Date.now() - new Date(lastSyncTime).getTime();
  const thresholdMs = 2 * EMAIL_SYNC_PERIOD_MIN * 60 * 1000; // 90s (2 × 45s cycle)
  if (syncAgeMs < thresholdMs) {
    if (DEBUG) console.log("Onboarding incomplete, first catchup burst — forcing catchup mode");
    lastSyncTime = null;
  }
}
```
- Only resets `lastSyncTime` when ALL of: onboarding incomplete, folder discovery hasn't succeeded yet, AND last sync was < 90s ago (2 × 45s cycle)
- After the first catchup burst (Inbox fills), incremental mode kicks in. When folder discovery succeeds on a later cycle, subfolders get their own catchup naturally (no prior sync state per-folder)
- Once `hasCompletedFolderSync` is true (either from discovery or seeded from profile), this block never fires again

**3e.** After sync loop completes (line ~1131, after `updateHeartbeat`):
```js
if (hasCompletedFolderSync && profile && !profile.onboarding_completed_at && !profile.initial_sync_complete) {
  setInitialSyncComplete(userId).catch((err) => {
    console.warn("[Clarion] Failed to set initial_sync_complete:", err.message);
  });
}
```
- Guards: only fires when discovery succeeded, onboarding not done, flag not already set in DB
- **PATCH failure handling:** If `setInitialSyncComplete` fails (network blip, auth expiry), the in-memory `hasCompletedFolderSync` stays true but `profile.initial_sync_complete` remains false in DB. On next cycle, `getProfile()` re-fetches the profile (line 956), condition `!profile.initial_sync_complete` is still true → retry. Self-healing.

### supabase-rest.js

**3f.** Add `setInitialSyncComplete(userId)` helper (after `updateHeartbeat`):
```js
async function setInitialSyncComplete(userId) {
  return supabaseRequest(`/profiles?id=eq.${userId}&initial_sync_complete=eq.false`, {
    method: "PATCH",
    body: { initial_sync_complete: true },
  });
}
```
- `eq.false` filter = idempotent no-op if already set

**3g.** Update `getProfile` select list (line 75):
- Add `initial_sync_complete` to the select string

---

## Change 4: Extension — Larger Per-Folder Cap During Initial Sync

**File:** `extension/background.js`

### Problem

The extension syncs 50 emails/folder/cycle (incremental mode). With 3 folders, that's ~150 emails/cycle on a 45s interval. Reaching 500 emails takes ~150+ seconds minimum — during which the worker may poll and find the user eligible under the old fallback rule. Even with the new gating, reaching 500 slowly is a poor onboarding UX.

### Solution

**4a.** Where `maxEmails` is determined per sync cycle (around lines 990-1000), add a condition for pre-onboarding initial sync:

```js
const isInitialSync = profile && !profile.onboarding_completed_at && !hasCompletedFolderSync;
const maxEmails = isInitialSync ? 200 : (isCatchup ? 1000 : 50);
```

- **200/folder during initial sync** — 3 folders × 200 = 600 emails per cycle, enough to cross the 500 threshold in 1-2 cycles
- Falls back to normal 50 (incremental) or 1000 (catchup) once `hasCompletedFolderSync` is true
- This is a targeted boost, not a permanent change — only fires during the pre-onboarding window

**4b.** After `hasCompletedFolderSync` is set to true (from discovery success in 3c), subsequent cycles use the normal cap logic. No persistent state needed.

---

## Change 5: Worker — Gate Onboarding + Remove Premature Fallback

**File:** `worker/supabase_client.py`

### Problem

`get_users_needing_onboarding()` (line 370) has a fallback: `email_count >= 5 AND account_age > 3 days`. This means a user with only 5 emails and a 3-day-old account can trigger onboarding, producing guides from nearly no data. The `initial_sync_complete` gate helps, but if the extension sets that flag after discovering folders + syncing just a handful of emails, the fallback still fires.

### Solution

**5a.** In `get_users_needing_onboarding()` (line 382):
- Add `initial_sync_complete` to the `.select()` query

**5b.** In the eligibility loop (after line 395), add:
```python
if not row.get("initial_sync_complete"):
    continue
```

**5c.** Remove the time-based fallback (lines 409-414). Replace with a single check:
```python
if email_count >= min_emails:
    ready.append(uid)
```

The fallback was designed for "quiet inboxes" that might never reach 500 emails. But triggering onboarding on 5 emails produces unusable guides. Instead, if a user genuinely has < 500 emails after extended sync, we should handle that downstream (see Change 6 — the sent email minimum gate).

**5d.** Update the function signature and docstring:
```python
def get_users_needing_onboarding(self, min_emails=500):
```
Remove `fallback_emails` and `fallback_days` parameters.

**Race condition mitigation:** The `initial_sync_complete` flag is only set **after** the sync loop finishes writing all emails (step 3e runs after all `pushEmails` calls). So the "flag set but emails not landed" race is structurally prevented.

---

## Change 6: Worker — Minimum Sent Email Gate for Guides

**File:** `worker/onboarding/runner.py`

### Problem

The style guide and behavioral profile each sample up to 120 **sent** emails (`extraction.py:138`). If a user has 480 received and 20 sent emails, they pass the 500 total gate but produce thin, unreliable guides. The current check only validates `len(received) >= 10` (line 82) — there's no minimum for sent.

### Solution

**6a.** After the received email check (line 82-85), add a sent email minimum:
```python
MIN_SENT_FOR_GUIDES = 30

if len(sent) < MIN_SENT_FOR_GUIDES:
    logger.warning(
        f"Only {len(sent)} sent emails (need {MIN_SENT_FOR_GUIDES}) — "
        f"onboarding will complete without style/behavior guides"
    )
    skip_guides = True
else:
    skip_guides = False
```

**6b.** In Phase 4C (around line 300, where style guide + behavioral profile are generated), wrap the guide synthesis in the `skip_guides` check:
```python
if skip_guides:
    logger.info("Skipping style guide + behavioral profile (insufficient sent emails)")
    style_guide = None
    behavioral_profile = None
else:
    # existing synthesis code...
```

**6c.** This allows onboarding to complete as `complete_partial` (lines 322-387 already handle missing guides gracefully). The user gets basic functionality without malformed guides. When they accumulate more sent emails, a future re-profiling pass can generate proper guides.

### Why 30?

- The 120-email sample uses stratified bucketing across a 3×2 grid (body length × contact type = 6 buckets)
- With 30 sent emails, each bucket gets ~5 emails on average — enough for Haiku to extract meaningful patterns
- Below 30, most buckets would have 0-2 emails, producing guide text that's speculation rather than observation

---

## Change 7: Worker — Over-fetch, Filter, Then Cap at 500

**File:** `worker/onboarding/runner.py`

**7a.** Line 78: Change `max_emails=500` → `max_emails=1500`

The 3x multiplier is a starting estimate. Typical corporate inbox noise (newsletters, noreply, calendar, auto-replies) is ~30-40%. At 1500 fetch → ~900-1050 clean → cap at 500 = comfortable headroom. For heavy-spam users (60%+), 1500 yields ~600, still above 500.

**File:** `worker/onboarding/collectors.py`

**7b.** After `pre_filter_emails()` (line 97-98), before the return, add a sent-preserving cap:
```python
MAX_ONBOARDING_EMAILS = 500
MAX_SENT_RESERVED = 150  # never discard sent emails that downstream phases need

total_clean = len(received) + len(sent)
if total_clean > MAX_ONBOARDING_EMAILS:
    # Reserve all sent emails (up to MAX_SENT_RESERVED), fill remaining with most recent received
    kept_sent = sorted(sent, key=lambda e: e.get("received_time") or "", reverse=True)[:MAX_SENT_RESERVED]
    remaining_slots = MAX_ONBOARDING_EMAILS - len(kept_sent)
    kept_received = sorted(received, key=lambda e: e.get("received_time") or "", reverse=True)[:remaining_slots]
    received = kept_received
    sent = kept_sent
    logger.info(
        f"Capped to {MAX_ONBOARDING_EMAILS} clean emails "
        f"(sent floor: {len(sent)}, received: {len(received)})"
    )
```

**Why sent-first:** A naive recency sort across all emails disproportionately trims sent emails because users receive far more than they send. If someone has 900 received and 150 sent after filtering, the most recent 500 could easily skew to 470/30 — barely clearing the Change 6 gate. By reserving sent emails first (up to 150, matching the `sample_unified_sent_emails` max of 120 with headroom), we ensure the downstream style and behavioral extraction phases always have the data they need.

**7c.** Add filter ratio logging (after line 98, before the cap):
```python
logger.info(
    f"Pre-filter: {total} fetched, {filtered_count} removed "
    f"({filtered_count/total*100:.0f}% noise), {len(received)+len(sent)} clean"
)
```
This logs the actual noise ratio per user so we can validate the 3x multiplier against real data and adjust if needed.

---

## Guide Quality Cascade (Changes 6 → 8 → 10)

Changes 6, 8, and 10 form a three-layer cascade that progressively gates guide quality. Each layer catches a different failure mode:

```
Layer 1 — Hard gate (Change 6): ≥30 sent emails in corpus
  │  Catches: insufficient raw data (user doesn't send much email)
  │  Action: skip_guides = True → onboarding completes as complete_partial
  ▼
Layer 2 — Hard gate (Change 8): ≥15 style features / ≥10 behavioral features
  │  Catches: Haiku extraction failures (network errors, malformed responses)
  │  Action: synthesis returns None → onboarding completes as complete_partial
  ▼
Layer 3 — Soft constraint (Change 10): prompt hedging below 30/20 features
  │  Catches: thin-but-sufficient data where patterns exist but aren't robust
  │  Action: Sonnet uses qualified language, avoids overgeneralization
  ▼
  Normal synthesis (adequate data)
```

**Implementation note:** Add a one-liner comment at each checkpoint in the code referencing this cascade, so someone implementing without the plan understands why there are three overlapping checks at different thresholds:
- `runner.py` (Change 6 gate): `# Guide quality cascade Layer 1: raw sent email minimum (see plan §cascade)`
- `synthesis.py` (Change 8 gates): `# Guide quality cascade Layer 2: extracted feature minimum (see plan §cascade)`
- `synthesis.py` (Change 10 prompt logic): `# Guide quality cascade Layer 3: soft prompt constraint (see plan §cascade)`

---

## Change 8: Worker — Minimum Extracted-Feature Threshold Before Sonnet Synthesis

**Files:** `worker/onboarding/synthesis.py`

### Problem

`synthesize_style_guide()` (line 125) only checks `if not style_features` — i.e., whether the list is empty. If even 1 Haiku extraction succeeds, Sonnet receives it and generates a full 300-500 word guide from that single data point. Same for `synthesize_behavioral_profile()` (line 181).

This is a separate issue from the sent-email gate in Change 6. Even with 30+ sent emails, Haiku batch extraction can partially fail (network errors, malformed responses), leaving only a handful of successfully extracted features.

### Solution

**8a.** In `synthesize_style_guide()` (after the empty check at line 125), add a minimum threshold:
```python
MIN_STYLE_FEATURES = 15

if not style_features:
    logger.warning("No style features to synthesize")
    return None, {}

if len(style_features) < MIN_STYLE_FEATURES:
    logger.warning(
        f"Only {len(style_features)} style features extracted "
        f"(need {MIN_STYLE_FEATURES}) — skipping style guide synthesis"
    )
    return None, {}
```

**8b.** In `synthesize_behavioral_profile()` (after the empty check at line 181), add the same pattern:
```python
MIN_BEHAVIORAL_FEATURES = 10

if not behavioral_features:
    logger.warning("No behavioral features to synthesize")
    return None, {}

if len(behavioral_features) < MIN_BEHAVIORAL_FEATURES:
    logger.warning(
        f"Only {len(behavioral_features)} behavioral features extracted "
        f"(need {MIN_BEHAVIORAL_FEATURES}) — skipping behavioral profile synthesis"
    )
    return None, {}
```

### Why these thresholds?

- **Style (15):** The style guide covers 9 dimensions (greetings, sign-offs, sentence structure, formality spectrum, pleasantries, common phrases, request handling, punctuation, notable markers). At 15 features, each dimension has ~1-2 data points on average — still thin, but enough for Sonnet to identify real patterns rather than overfitting to a single email.
- **Behavioral (10):** The behavioral profile covers 4 dimensions (decision disposition, response completeness, commitment patterns, scope behavior). At 10 features, each dimension has ~2-3 data points. Below 10, Sonnet can't distinguish between the user's actual patterns and noise from a single unusual email.

---

## Change 9: Worker — Track Extracted Feature Count in DB

**File:** `worker/onboarding/runner.py`

### Problem

`runner.py:341` records `style_result.get("sample_count")` as `style_sample_count` in the DB. But `sample_count` is the number of emails *sampled* (input to Haiku), not the number that successfully yielded features. If 120 emails are sampled but Haiku extraction fails on 90% of batches, the DB records 120 while only 12 features were actually extracted.

This makes it impossible to diagnose quality issues from the DB alone — a user with `style_sample_count = 120` looks well-profiled even if their guide was built from 3 features.

### Solution

**9a.** In `runner.py`, where `sample_count` is recorded (around line 341), change to track the actual extracted count:
```python
if style_guide:
    extracted_count = len(style_result.get("style_features", [])) if style_result else 0
    sampled_count = style_result.get("sample_count", 0) if style_result else 0
    db.update_writing_style(user_id, style_guide, extracted_count)
    logger.info(
        f"Style guide saved: {extracted_count} features extracted "
        f"from {sampled_count} sampled emails"
    )
```

**9b.** Rename the column to match its new meaning. Add to the migration in Change 1 (or a separate migration):
```sql
ALTER TABLE public.profiles
  RENAME COLUMN style_sample_count TO style_extracted_feature_count;
```

**Why rename instead of document:** A silent semantic shift (column means X now instead of Y) is a landmine. Anyone querying the DB — dashboards, debugging queries, future developers — won't know the meaning changed unless they read this plan. Column names should be self-documenting. The rename requires updating references in `supabase_client.py:update_writing_style()` (line 569) and any dashboard queries that read `style_sample_count`.

**9c.** Update `supabase_client.py:update_writing_style()` to use the new column name:
```python
def update_writing_style(self, user_id, style_guide, extracted_count):
    self.client.table("profiles").update({
        "writing_style_guide": style_guide,
        "style_profiled_at": datetime.utcnow().isoformat(),
        "style_extracted_feature_count": extracted_count,
    }).eq("id", user_id).execute()
```

---

## Change 10: Worker — Add Sample-Size Awareness to Sonnet Synthesis Prompts

**File:** `worker/onboarding/prompts.py`

### Problem

`SONNET_STYLE_GUIDE_PROMPT` and `SONNET_BEHAVIORAL_PROFILE_PROMPT` don't tell Sonnet how many data points it's working with or how to handle small samples. Sonnet will confidently generate rules like "The user always uses 'Thanks!' as a greeting" from 3 emails where it happened to appear.

This produced the exact output the user reported: guides full of confident claims hedged only by Sonnet's own uncertainty ("Limited data shows...") rather than being structurally constrained.

### Solution

**10a.** In `synthesis.py`, update the user prompt for `synthesize_style_guide()` (line 145-148) to include sample size context:
```python
prompt_text = (
    f"Writing pattern analysis from {len(enriched)} sent emails"
    f" (sample size: {'small — focus on consistent patterns only, '
    'avoid generalizing from single occurrences' if len(enriched) < 30
    else 'adequate'}):\n\n"
    + json.dumps(enriched)
)
```

**10b.** Same for `synthesize_behavioral_profile()` (line 203-207):
```python
prompt_text = (
    f"Behavioral pattern analysis from {len(behavioral_features)} sent emails"
    f" (sample size: {'small — only report patterns observed in 3+ emails, '
    'state \"insufficient data\" for dimensions without clear patterns'
    if len(behavioral_features) < 20 else 'adequate'}):\n\n"
    + json.dumps(behavioral_features)
    + profiles_block
)
```

**10c.** Add a reinforcing instruction to `SONNET_STYLE_GUIDE_PROMPT` in `prompts.py` (after the existing "IMPORTANT:" block around line 217):
```
SAMPLE SIZE RULES:
- If the sample contains fewer than 30 emails, explicitly state which patterns \
are well-supported vs. tentative.
- Never use phrases like "always", "consistently", or "predominantly" for \
patterns observed in fewer than 5 emails — use "observed in N emails" instead.
- If a dimension lacks enough data to identify a pattern, write: "Insufficient \
data to determine [dimension] patterns." Do NOT speculate or fill in with \
generic professional defaults.
```

**10d.** Add equivalent to `SONNET_BEHAVIORAL_PROFILE_PROMPT` (after the "CRITICAL INSTRUCTIONS:" block around line 380):
```
SAMPLE SIZE RULES:
- If the sample contains fewer than 20 emails, only generate rules for \
dimensions where the pattern appears in 3+ emails.
- For dimensions with fewer than 3 supporting examples, write: "No consistent \
pattern observed for [dimension] due to insufficient data. The draft model \
should use neutral behavior."
- Never extrapolate a single email into a general rule.
```

### Why this matters

The new guides the user compared were full of hedging like "Limited data shows preference for 'Thanks!' as a multipurpose opener" — but this hedging was Sonnet's own judgment, not structurally enforced. Without explicit prompt instructions, Sonnet alternates between overconfident generalization and vague disclaimers unpredictably. These prompt changes make the behavior deterministic: small sample → constrained output.

---

## Files Modified

| File | Change |
|------|--------|
| `supabase/migrations/030_initial_sync_complete.sql` | **New** — add column |
| `extension/background.js` | Expand collapsed folders in discovery, add sync flag + seed from profile, set on discovery, signal to Supabase, fix catchup loop, larger initial sync cap |
| `extension/supabase-rest.js` | Add `setInitialSyncComplete()`, update `getProfile` select |
| `worker/supabase_client.py` | Gate onboarding on `initial_sync_complete`, remove fallback trigger, rename `style_sample_count` → `style_extracted_feature_count` in `update_writing_style()` |
| `worker/onboarding/runner.py` | Change `max_emails=500` → `1500`, add sent email minimum gate + `skip_guides` flag, track extracted (not sampled) feature count |
| `worker/onboarding/collectors.py` | Post-filter cap at 500 + filter ratio logging |
| `worker/onboarding/synthesis.py` | Add minimum feature thresholds before Sonnet calls, add sample-size context to user prompts |
| `worker/onboarding/prompts.py` | Add sample-size rules to `SONNET_STYLE_GUIDE_PROMPT` and `SONNET_BEHAVIORAL_PROFILE_PROMPT` |

## Edge Cases

- **User has no subfolders** — `discoverMailFolders()` succeeds (sidebar renders), expand step is a no-op (nothing collapsed). Returns only Inbox entries which get filtered. `hasCompletedFolderSync` set to `true` because discovery didn't throw.
- **Collapsed folders can't be expanded** — Chevron selector fails silently; falls back to current behavior (visible folders only). Not worse than today.
- **Sidebar never renders** — Discovery throws, flag stays `false`, onboarding stays gated. User re-opens Outlook, sidebar renders, flag set on next sync.
- **MV3 service worker killed** — On re-wake, `getProfile()` seeds `hasCompletedFolderSync` from `profile.initial_sync_complete` (step 3b). No redundant catchup burst.
- **PATCH failure** — In-memory flag stays true, but `profile.initial_sync_complete` stays false in DB. Next cycle re-fetches profile, condition still met → automatic retry.
- **Over-fetch returns < 500 clean emails** — Capping code is a no-op; all clean emails used.
- **User has very few sent emails (< 30)** — Onboarding proceeds but skips guide synthesis. Completes as `complete_partial`. User gets basic functionality; guides can be built later when more sent mail accumulates.
- **Existing already-onboarded users** — Unaffected; worker skips them before reaching the `initial_sync_complete` check.
- **User with quiet inbox never reaches 500** — Without the fallback trigger, onboarding won't fire. This is intentional: onboarding on < 500 emails produced unusable guides anyway. A future enhancement could add a UI prompt ("Sync more email to unlock personalization") rather than silently producing bad output.
- **Haiku extraction partially fails (e.g., 120 sampled, 12 extracted)** — The minimum feature thresholds in Change 8 catch this. Style guide requires ≥15 extracted features, behavioral requires ≥10. If below threshold, synthesis is skipped and onboarding completes as `complete_partial`. The DB records the actual extracted count (Change 9), not the sampled count, so quality issues are diagnosable.
- **Token expiry mid-sync** — If Outlook token expires between folder discovery and sync completion, individual folder syncs fail (caught per-folder at line 1070). `totalSynced` may be partial. However `initial_sync_complete` is only set after the sync loop exits normally (line 1131). If the top-level `try` catches a fatal error (line 1134), we hit the `catch` branch → flag never set → worker stays gated. Partial per-folder failures still set the flag, but the `email_count >= 500` gate prevents onboarding on too-thin data.

## Verification

1. **Migration:** Run `supabase db push` or apply manually; confirm column exists
2. **Folder expansion:** Open Outlook with collapsed subfolders, trigger sync, check console for `[Clarion] discoverMailFolders: found N treeitem elements` — N should be higher than before (includes children of expanded nodes)
3. **Extension folder sync:** Open Outlook with sidebar visible, trigger sync, check console for `[Clarion] Discovered N subfolders` and confirm `initial_sync_complete` flips to `true` in profiles table
4. **Initial sync throughput:** Verify console shows `maxEmails=200/folder` during pre-onboarding cycles, drops to 50 after `hasCompletedFolderSync` is set
5. **Worker gating:** With `initial_sync_complete = false`, confirm worker skips the user. Set to `true` with 500+ emails, confirm onboarding triggers
6. **Fallback removed:** Confirm user with 5 emails + 3-day account + `initial_sync_complete = true` does NOT trigger onboarding
7. **Sent email gate:** Test with < 30 sent emails: confirm guides are skipped, onboarding completes as `complete_partial`, logs show warning
8. **Email collection:** Check logs for filter ratio line (e.g., "Pre-filter: 1500 fetched, 450 removed (30% noise), 1050 clean"). Verify downstream phases receive correct data shape.
9. **MV3 kill recovery:** After initial sync, kill the service worker from `chrome://extensions`. On re-wake, confirm `hasCompletedFolderSync` is seeded from profile (no catchup burst in console).
10. **Feature threshold gate:** Simulate partial Haiku failure (e.g., kill network mid-extraction). Confirm that if < 15 style features or < 10 behavioral features are extracted, synthesis is skipped, logs show warning, and onboarding completes as `complete_partial`.
11. **Extracted count in DB:** After successful onboarding, query `profiles.style_extracted_feature_count` and verify it matches the number of features extracted (from logs), not the number of emails sampled.
12. **Small sample prompt behavior:** Run onboarding with 20-29 sent emails (above the 15-feature threshold but below 30). Verify the generated style guide uses qualified language ("observed in N emails") rather than absolute claims ("always", "consistently").
