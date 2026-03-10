# Implementation Guide: Fix Audit Bugs Before Onboarding Rerun

Three bugs found during the deep audit. All must be fixed before re-running onboarding.

---

## Bug 1 (BLOCKER): 6 Missing Columns in `contacts` Table

**Problem:** `upsert_contacts()` in `supabase_client.py:401-426` writes these 6 columns that don't exist in the DB:
- `reply_rate_30d`
- `reply_rate_90d`
- `smoothed_rate`
- `median_response_time_hours`
- `forward_rate`
- `typical_subjects`

Every onboarding run will crash on the upsert.

### Fix: Two-part

#### Part A — Migration `009_contacts_missing_columns.sql`
Create `supabase/migrations/009_contacts_missing_columns.sql`:

```sql
-- Migration 009: Add missing contacts columns required by upsert_contacts().
--
-- These columns are populated during onboarding synthesis and used by the
-- scorer for per-sender response modeling. Without them, upsert_contacts()
-- fails because Postgres rejects writes to non-existent columns.
--
-- Note: DDL in Postgres is auto-transactional — if any ALTER fails, the
-- entire statement rolls back. The explicit BEGIN/COMMIT is belt-and-suspenders
-- and doesn't change behavior here, but documents intent.

BEGIN;

ALTER TABLE public.contacts
  ADD COLUMN IF NOT EXISTS reply_rate_30d     double precision,
  ADD COLUMN IF NOT EXISTS reply_rate_90d     double precision,
  ADD COLUMN IF NOT EXISTS smoothed_rate      double precision,
  ADD COLUMN IF NOT EXISTS median_response_time_hours double precision,
  ADD COLUMN IF NOT EXISTS forward_rate       double precision,
  ADD COLUMN IF NOT EXISTS typical_subjects   text[] DEFAULT '{}';

COMMENT ON COLUMN public.contacts.reply_rate_30d IS
  'Fraction of emails from this sender the user replied to in the last 30 days';
COMMENT ON COLUMN public.contacts.reply_rate_90d IS
  'Fraction of emails from this sender the user replied to in the last 90 days';
COMMENT ON COLUMN public.contacts.smoothed_rate IS
  'Bayesian-smoothed response rate: (raw_rate * n + prior_weight * global_rate) / (n + prior_weight)';
COMMENT ON COLUMN public.contacts.median_response_time_hours IS
  'Median hours between receiving an email from this sender and the user replying';
COMMENT ON COLUMN public.contacts.forward_rate IS
  'Fraction of emails from this sender the user forwarded rather than replied to';
COMMENT ON COLUMN public.contacts.typical_subjects IS
  'Array of representative subject lines from this sender, used for topic matching';

COMMIT;
```

**Deploy:** Run this SQL in Supabase SQL Editor **before** pushing any code changes.

#### Part B — No code changes needed
`upsert_contacts()` already populates these fields from the onboarding synthesis output. Once the columns exist, the upserts will succeed.

---

## Bug 2 (HIGH): Thread Context Dead — Wrong Table Read

**Problem:** Two methods read from the `conversations` table, which is **always empty**. Meanwhile, the `threads` table has real aggregate data (populated by onboarding via `upsert_threads()`), but is never read at runtime.

Affected methods:
1. `fetch_thread_messages()` (`supabase_client.py:501-521`) — used by scoring pipeline
2. `fetch_conversation_context()` (`supabase_client.py:242-260`) — used by draft generation

### Fix 2A: Rename and rewire `fetch_thread_messages()` → `fetch_thread_stats()`

**File:** `worker/supabase_client.py` lines 501-521

**Current code** reads from `conversations` table expecting `messages` jsonb:
```python
def fetch_thread_messages(self, user_id, conversation_ids):
    ...
    result = (
        self.client.table("conversations")
        .select("conversation_id, messages, updated_at")
        .eq("user_id", user_id)
        .in_("conversation_id", unique)
        .execute()
    )
    return {row["conversation_id"]: row for row in (result.data or [])}
```

**Replace with** — renamed method, reads from `threads` table using `select("*")`:
```python
def fetch_thread_stats(self, user_id, conversation_ids):
    """Batch-fetch thread aggregate stats for multiple conversation IDs.

    Reads from the threads table, which stores per-thread aggregates
    populated during onboarding (total_messages, participation_rate, etc.).

    Uses select("*") — the threads table is narrow and all columns are
    relevant to scoring. If columns are added to threads in the future,
    they'll be included automatically. Safe because all downstream consumers
    (_build_thread_info, _update_response_labels) use .get() with defaults
    and never iterate over row keys.

    Returns:
        dict: {conversation_id: thread_row} with aggregate stats.
    """
    if not conversation_ids:
        return {}
    unique = list(set(conversation_ids))
    result = (
        self.client.table("threads")
        .select("*")
        .eq("user_id", user_id)
        .in_("conversation_id", unique)
        .execute()
    )
    return {row["conversation_id"]: row for row in (result.data or [])}
```

**Call site update** — `run_pipeline.py` line 556:
```python
# Before:
threads_map = db.fetch_thread_messages(user_id, conv_ids)

# After:
threads_map = db.fetch_thread_stats(user_id, conv_ids)
```

**`select("*")` safety note:** Confirmed that the scorer (`scorer.py`) only accesses `thread_info` via `.get()` with explicit key names (lines 253, 275, 289, 308, 314, 366-367). No downstream code iterates over thread_row keys or treats arbitrary fields as scorer inputs. Extra columns from PostgREST (`id`, `created_at`, `updated_at`, `user_id`) are harmless.

### Fix 2B: Leave `fetch_conversation_context()` as-is (for now)

This method feeds `conversation_history` into the draft generation prompt. The `conversations` table would need per-message data (subject, body, sender, timestamp) to build useful context. The `threads` table only has aggregates, so it can't replace this.

**Impact:** Draft generation won't have conversation history — same as today (since conversations is empty). Not a regression. See "Technical Debt" section below.

---

## Bug 3 (MEDIUM): `_build_thread_info()` Expects Per-Message Data

**Problem:** `_build_thread_info()` in `run_pipeline.py:563-637` expects `thread_row["messages"]` to be a list of per-message dicts. After Fix 2A, `thread_row` will instead have aggregate fields directly (`total_messages`, `user_messages`, `participation_rate`, etc.).

Two downstream consumers of `threads_map`:
1. `_build_thread_info()` — builds scorer input (lines 563-637)
2. `_update_response_labels()` — retroactively labels responses (lines 640-669)

### Fix 3A: Rewrite `_build_thread_info()` to use aggregates

**File:** `worker/run_pipeline.py` lines 563-637

**Replace entire function with:**
```python
def _build_thread_info(email_data, thread_row, contact, user_aliases):
    """Build thread_info dict for the scorer from DB thread data.

    Args:
        email_data: dict from supabase_row_to_email_data().
        thread_row: dict from threads table (aggregate stats) or None.
        contact: dict from contacts table (or None).
        user_aliases: list[str] of user email addresses.

    Returns:
        dict: thread_info for score_email().
    """
    info = {
        "total_messages": 1,
        "user_messages": 0,
        "participation_rate": None,
        "user_initiated": False,
        "hours_since_user_reply": None,
        "sender_events_count": None,
    }

    if contact:
        info["sender_events_count"] = contact.get("total_received")

    if not thread_row:
        conv_id = email_data.get("conversation_id")
        if conv_id:
            logger.debug(f"No thread stats for conversation_id={conv_id}")
        return info

    # Read directly from thread aggregate fields
    info["total_messages"] = thread_row.get("total_messages", 1)
    info["user_messages"] = thread_row.get("user_messages", 0)
    info["participation_rate"] = thread_row.get("participation_rate")
    info["user_initiated"] = thread_row.get("user_initiated", False)

    # hours_since_user_reply cannot be computed from aggregates alone
    # (would need per-message timestamps). Leave as None — scorer treats
    # None as "no data" and skips the recency multiplier.
    # See "Technical Debt" section — populating the conversations table
    # will restore this feature.

    return info
```

**What's lost:**
- `hours_since_user_reply` — requires per-message timestamps, not available in aggregates. Scorer handles `None` gracefully (skips thread recency multiplier). Acceptable trade-off until `conversations` table is populated.

### Fix 3B: `_update_response_labels()` — No code change needed

The existing code already handles missing `messages` gracefully:
```python
messages = thread_row.get("messages") or []
if len(messages) < 2:
    continue
```
`thread_row.get("messages")` returns `None` on aggregate rows → `messages = []` → `continue`. Naturally becomes a no-op.

---

## Summary of Changes

| # | Type | File | Action |
|---|------|------|--------|
| 1 | SQL | `supabase/migrations/009_contacts_missing_columns.sql` | Create — add 6 columns with comments |
| 2 | Code | `worker/supabase_client.py` lines 501-521 | Edit — rename to `fetch_thread_stats()`, read from `threads` with `select("*")` |
| 3 | Code | `worker/run_pipeline.py` line 556 | Edit — update call site: `fetch_thread_messages` → `fetch_thread_stats` |
| 4 | Code | `worker/run_pipeline.py` lines 563-637 | Edit — use aggregate fields, add debug log for missing threads |

**No changes needed:**
- `fetch_conversation_context()` — same behavior as today (returns empty), not a regression
- `_update_response_labels()` — naturally becomes no-op without `messages` key

---

## Pre-flight: Onboarding Idempotency Check

Before re-running onboarding, run these queries and follow the decision tree:

```sql
-- 1. Check contacts state
SELECT COUNT(*) AS contact_count, MAX(last_profiled_at) AS last_profiled
FROM public.contacts
WHERE user_id = '92ed5164-03ef-480c-a1bd-449236ed856e';

-- 2. Check threads state
SELECT COUNT(*) AS thread_count
FROM public.threads
WHERE user_id = '92ed5164-03ef-480c-a1bd-449236ed856e';

-- 3. Check onboarding status
SELECT onboarding_status, onboarding_started_at, onboarding_completed_at
FROM public.profiles
WHERE id = '92ed5164-03ef-480c-a1bd-449236ed856e';
```

### Decision tree

| contacts | threads | onboarding_status | What happened | Action |
|----------|---------|-------------------|---------------|--------|
| 0 | 0 | `NULL` or `failed` | Never ran or crashed before collecting | Safe to run. No cleanup needed. |
| >0 | 0 | `failed` | Crashed after contacts upsert, before threads upsert | Safe to re-run. Contacts will be overwritten via `ON CONFLICT DO UPDATE`. |
| >0 | >0 | `failed` | Crashed after threads (e.g., during model training) | Safe to re-run. Both tables use `ON CONFLICT DO UPDATE`, stale rows get overwritten. |
| >0 | >0 | `complete` | Previous onboarding succeeded | **Do not re-run** unless intentional. If you want a fresh onboarding, first reset: `UPDATE profiles SET onboarding_status = NULL, onboarding_completed_at = NULL WHERE id = '...'` |
| any | any | in-progress status (`collecting`, `extracting`, etc.) | Onboarding is currently running or crashed without updating status to `failed` | Wait for it to finish, or manually set status to `failed` before re-running: `UPDATE profiles SET onboarding_status = 'failed' WHERE id = '...'` |

**Why re-runs are safe:** Both `upsert_contacts()` and `upsert_threads()` use `ON CONFLICT ... DO UPDATE`, so partial data from a crashed run gets overwritten, never duplicated.

---

## Onboarding / Scoring Pipeline Race Condition

**Question:** Can the scoring pipeline process emails for a user while onboarding is running?

**Answer: No, in the current architecture.** The worker is single-threaded. The main loop in `main.py:214-300` runs sequentially:
1. Onboarding check (line 217) — blocks until complete
2. Model re-training check (line 229)
3. Stuck email recovery (line 240)
4. Accumulation window (line 249) — only starts after onboarding finishes

So if onboarding runs for user X, the accumulation phase (which claims and scores emails) doesn't start until onboarding returns. New emails synced by the extension during onboarding just accumulate as `unprocessed` rows and get picked up in the next loop iteration.

**Edge case to watch:** If you ever move to a multi-worker or async architecture, this assumption breaks. The `_is_user_active()` check (line 99) only looks at `worker_active` — it does **not** check `onboarding_status`. A future guard would be:
```python
if profile.get("onboarding_status") not in (None, "complete", "failed"):
    return False  # skip user during active onboarding
```
Not needed now, but worth flagging for future architecture changes.

---

## Score Distribution Impact Analysis

**What changes:** Going from all-defaults (`total_messages=1`, `participation_rate=None`, `user_initiated=False`) to real thread data affects these scorer steps:

| Scorer step | Default behavior (before) | With real data (after) | Impact |
|-------------|--------------------------|----------------------|--------|
| Step 5: depth_mult | `depth=1` → lookup returns ~1.0 | Real depth (e.g. 5, 10) → could be >1.0 or <1.0 depending on trained bins | **Medium** — multi-message threads will score differently |
| Step 7: participation penalty | `participation_rate=None` → skipped entirely | Real rate → could apply 0.5x or 0.75x penalty for low-participation threads with depth>2 | **High** — some threads will get penalized that weren't before |
| Step 9: cold_start | `sender_events_count=None` → skipped | Real count from `total_received` (already fixed by migration 008) | Already live |
| Step 10: thread_recency | `hours_since_user_reply=None` → skipped | Still None (can't compute from aggregates) | **No change** |

**Recommendation:** Before flipping live, score a sample batch under both conditions:

```sql
-- Get 20 recent scored emails with their conversation_ids
SELECT e.id, e.sender_email, e.subject, e.conversation_id,
       re.raw_score, re.calibrated_prob, re.scoring_factors
FROM public.emails e
JOIN public.response_events re ON re.email_id = e.id
WHERE e.user_id = '92ed5164-03ef-480c-a1bd-449236ed856e'
  AND re.raw_score IS NOT NULL
ORDER BY e.received_time DESC
LIMIT 20;
```

After deploying but before re-running onboarding, manually re-score a few of these emails (via a test script or by resetting them to `unprocessed`) and compare `raw_score` values. If scores shift dramatically (>2x change on average), consider recalibrating thresholds (`soft_gate_threshold`, `hard_gate_threshold`) before going live.

**If thresholds need adjustment:** The scorer's `check_triage_gate()` (scorer.py:352) uses `soft_gate_threshold` (default 0.10) and `hard_gate_threshold` (default 0.05). These were tuned against the all-defaults regime. With real thread data flowing, you may want to lower them temporarily and observe draft generation rates before dialing them back up.

---

## Deployment Order (Migration Before Code)

**Critical:** Run the migration **before** pushing code changes.

1. **Run migration 009** in Supabase SQL Editor — adds the 6 columns
2. **Edit the two Python files** locally
3. **Push to GitHub** (`git add -A && git commit && git push`)
4. **Redeploy Railway** — from Railway dashboard, three-dot menu → Redeploy
5. **Vercel** — no web changes this time

**Why this order:** If code deploys before the migration, the old code still runs fine (it was already crashing on upsert_contacts — no regression). The Fix 2A code change is also safe against the old schema because the `threads` table already exists. But running the migration first is the clean path.

If Railway auto-deploys unexpectedly on push, this is still safe:
- `fetch_thread_stats()` reads from `threads` (already exists and populated)
- `_build_thread_info()` reads aggregate fields (already in `threads`)
- The 6 missing columns only matter when onboarding runs, which you trigger manually

## Rollback Plan

If the onboarding re-run surfaces new bugs downstream of these fixes:

- **Code:** `git revert <sha> && git push`, then redeploy Railway
- **Migration 009 columns:** Leave in place — additive, nullable, harmless. No data depends on them if onboarding is reverted.
- **Net effect:** System returns to pre-fix state (thread scoring dead, upsert_contacts still broken). No data loss.

---

## Post-Deploy Monitoring

### Immediate verification (after deploy, before onboarding)
1. Check worker logs for `No thread stats for conversation_id=` debug lines — confirms new code path is active
2. Run `SELECT column_name FROM information_schema.columns WHERE table_name = 'contacts' ORDER BY ordinal_position;` — confirm 6 new columns

### After onboarding completes
1. Spot-check a known sender: `SELECT email, smoothed_rate, reply_rate_30d, reply_rate_90d, median_response_time_hours, forward_rate FROM contacts WHERE user_id = '92ed5164...' AND email = 'jim.crigler@hc2capital.com';`
2. Compare a few scores against the pre-fix batch (see Score Distribution Impact Analysis above)

### Weekly data quality checks (ongoing)
Run these periodically to catch drift or bad data before it degrades scoring silently:

```sql
-- Sanity: rates should be between 0.0 and 1.0
SELECT email, smoothed_rate, reply_rate_30d, reply_rate_90d, forward_rate
FROM public.contacts
WHERE user_id = '92ed5164-03ef-480c-a1bd-449236ed856e'
  AND (smoothed_rate < 0 OR smoothed_rate > 1.0
       OR reply_rate_30d < 0 OR reply_rate_30d > 1.0
       OR reply_rate_90d < 0 OR reply_rate_90d > 1.0
       OR forward_rate < 0 OR forward_rate > 1.0);

-- Sanity: median response time should be positive and reasonable (<720 hours = 30 days)
SELECT email, median_response_time_hours
FROM public.contacts
WHERE user_id = '92ed5164-03ef-480c-a1bd-449236ed856e'
  AND (median_response_time_hours < 0 OR median_response_time_hours > 720);

-- Sanity: thread participation rate should be 0-1
SELECT conversation_id, participation_rate, total_messages, user_messages
FROM public.threads
WHERE user_id = '92ed5164-03ef-480c-a1bd-449236ed856e'
  AND (participation_rate < 0 OR participation_rate > 1.0
       OR user_messages > total_messages);

-- Stale data check: contacts not updated in >30 days despite active emails
SELECT c.email, c.updated_at, COUNT(e.id) AS recent_emails
FROM public.contacts c
LEFT JOIN public.emails e ON e.sender_email = c.email
  AND e.user_id = c.user_id
  AND e.received_time > NOW() - INTERVAL '30 days'
WHERE c.user_id = '92ed5164-03ef-480c-a1bd-449236ed856e'
GROUP BY c.email, c.updated_at
HAVING c.updated_at < NOW() - INTERVAL '30 days' AND COUNT(e.id) > 5;
```

---

## Migration Index

| # | File | Date | Purpose |
|---|------|------|---------|
| 001 | `001_initial_schema.sql` | — | Base tables: emails, profiles, contacts, conversations, response_events |
| 002 | `002_dashboard_policies.sql` | — | RLS policies for dashboard access |
| 003 | `003_heartbeat_columns.sql` | — | Add heartbeat tracking to profiles |
| 003 | `003_onboarding_schema.sql` | — | Onboarding status columns on profiles |
| 004 | `004_scoring_and_enrichment.sql` | — | Scoring columns on response_events |
| 005 | `005_fix_contacts_precision.sql` | — | Fix numeric precision on contacts |
| 006 | `006_reset_stuck_emails.sql` | — | RPC function to recover stuck processing emails |
| 006 | `006_trace_scoring_columns.sql` | — | Add trace columns for pipeline debugging |
| 007 | `007_profile_display_name.sql` | — | Display name on profiles |
| 007 | `007_response_events_features.sql` | — | Boolean feature columns on response_events |
| 008 | `008_contacts_total_received.sql` | — | Add total_received to contacts + backfill |
| 009 | `009_contacts_missing_columns.sql` | pending | Add 6 missing contacts columns for onboarding |

**Note:** There are duplicate 003, 006, and 007 numbers. Consider adopting unique numbering going forward (e.g., timestamps like `20260310_001_description.sql`).

---

## Technical Debt: `conversations` Table

Deferred twice in this guide (Fix 2B and `hours_since_user_reply`). Tracking here so it doesn't drop off the radar.

**What's missing:** The `conversations` table exists but is never populated. It was designed to hold per-message data (sender, body, timestamp per message in a thread). This data would enable:
- `hours_since_user_reply` in the scorer (thread recency multiplier — up to 1.5x lift)
- `conversation_history` in draft generation (richer context for drafts)
- `_update_response_labels()` retroactive labeling (currently a no-op)

**What it would take:**
- Chrome extension changes to forward full thread message content (sender, body snippet, timestamp) when syncing emails
- A new ingestion path in `supabase_client.py` to write per-message rows to `conversations`
- Updates to `fetch_conversation_context()` and `_build_thread_info()` to use the real data

**Priority:** Medium. The system works without it — scores use aggregates, drafts use email body only. But draft quality and scoring accuracy both improve with per-message context. Worth a ticket once the current fixes are stable.
