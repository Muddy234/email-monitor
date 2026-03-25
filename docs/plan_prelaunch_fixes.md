# Pre-Launch Implementation Plan

## Priority 1 — Fix Before Launch

### 1.1 Onboarding blocks users with <20 emails

**Problem:** `get_users_needing_onboarding(min_emails=20)` in `worker/supabase_client.py:332` requires 20 emails before onboarding starts. A user with a quiet inbox is stuck forever — emails never get processed because onboarding never completes.

**Fix:**
- Add a time-based fallback: if account is >3 days old, allow onboarding with as few as 5 emails
- In `get_users_needing_onboarding()`, check `profiles.created_at` alongside email count
- New logic: `(email_count >= 20) OR (email_count >= 5 AND account_age > 3 days)`
- If user has 0 emails after 3 days, skip (extension probably not installed)

**Files:** `worker/supabase_client.py:332-364`

---

### 1.2 Migration numbering collisions

**Problem:** Four migration numbers are duplicated:
- `003_heartbeat_columns.sql` / `003_onboarding_schema.sql`
- `006_reset_stuck_emails.sql` / `006_trace_scoring_columns.sql`
- `007_profile_display_name.sql` / `007_response_events_features.sql`
- `014_feedback_table.sql` / `014_trial_ends_at.sql`

All use `IF NOT EXISTS` so they're idempotent, but execution order is undefined. A fresh deploy could break.

**Fix:** Rename duplicates to restore unique sequential ordering. Proposed mapping:

| Current | Rename to |
|---------|-----------|
| `003_heartbeat_columns.sql` | keep |
| `003_onboarding_schema.sql` | `003b_onboarding_schema.sql` |
| `006_reset_stuck_emails.sql` | keep |
| `006_trace_scoring_columns.sql` | `006b_trace_scoring_columns.sql` |
| `007_profile_display_name.sql` | keep |
| `007_response_events_features.sql` | `007b_response_events_features.sql` |
| `014_feedback_table.sql` | keep |
| `014_trial_ends_at.sql` | `014b_trial_ends_at.sql` |

Check which of each pair should run first (look for dependencies), then assign `a`/`b` accordingly.

**Files:** `supabase/migrations/` (renames only, no content changes)

**Note:** These migrations have already been applied. Renaming is for clarity and future fresh deploys only. Does not require re-running.

---

### 1.3 Contact stats N+1 queries

**Problem:** `bulk_upsert_contact_stats()` in `worker/supabase_client.py:740-790` runs one UPDATE query per existing contact. 30 emails from 20 known contacts = 20 individual round-trips.

**Fix:** Replace the per-row UPDATE loop with a single RPC call using a Postgres function:

```sql
CREATE FUNCTION increment_contact_stats(
    p_user_id uuid,
    p_contacts jsonb  -- [{email, name, contact_type, received_time}, ...]
) RETURNS void AS $$
    INSERT INTO contacts (user_id, email, name, contact_type, total_received, last_interaction_at, updated_at)
    SELECT
        p_user_id,
        c->>'email',
        c->>'name',
        c->>'contact_type',
        1,
        (c->>'received_time')::timestamptz,
        now()
    FROM jsonb_array_elements(p_contacts) AS c
    ON CONFLICT (user_id, email) DO UPDATE SET
        total_received = contacts.total_received + 1,
        last_interaction_at = GREATEST(contacts.last_interaction_at, EXCLUDED.last_interaction_at),
        updated_at = now();
$$ LANGUAGE sql;
```

Then replace the Python loop with a single `db.client.rpc("increment_contact_stats", {...}).execute()` call.

**Files:**
- New migration `023_increment_contact_stats_rpc.sql`
- `worker/supabase_client.py:740-790` — replace loop with RPC call

---

### 1.4 Thread email fetch N+1

**Problem:** `_fetch_thread_emails_batch()` in `worker/run_pipeline.py:549-570` runs one DB query per conversation_id. 100 conversations = 100 queries.

**Fix:** Replace the per-conversation loop with a single bulk query:

```python
def _fetch_thread_emails_batch(db, user_id, filtered_emails):
    conv_ids = list({
        ed["conversation_id"]
        for ed in filtered_emails
        if ed.get("conversation_id")
    })
    if not conv_ids:
        return {}

    result = (
        db.client.table("emails")
        .select("id, conversation_id, sender, sender_name, body, received_time, subject")
        .eq("user_id", user_id)
        .in_("conversation_id", conv_ids)
        .order("received_time", desc=True)
        .execute()
    )

    thread_emails_map = {}
    for row in (result.data or []):
        cid = row["conversation_id"]
        thread_emails_map.setdefault(cid, []).append(row)

    # Limit to 10 per conversation (matching original behavior)
    for cid in thread_emails_map:
        thread_emails_map[cid] = thread_emails_map[cid][:10]

    return thread_emails_map
```

**Concern:** Single query could return large result set if many conversations have long histories. Mitigate by keeping the `[:10]` trim per conversation. Supabase PostgREST has a default 1000-row limit; if >100 conversations × 10 emails, add `.limit(1000)` or paginate.

**Files:** `worker/run_pipeline.py:549-570`, `worker/supabase_client.py` (remove or keep `fetch_thread_emails` for other callers)

---

## Priority 2 — Address Soon After Launch

### 2.1 Sequential user processing

**Problem:** `worker/main.py:337-352` processes users one at a time. User A's batch blocks User B.

**Fix:** Use `concurrent.futures.ThreadPoolExecutor` with a configurable `MAX_WORKERS` (default 3). Each user gets their own DB client instance (thread-safe).

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = int(os.environ.get("MAX_PIPELINE_WORKERS", "3"))

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {}
    for user_id, data in accumulated.items():
        if not data["emails"] or not db.is_subscription_active(user_id):
            continue
        futures[executor.submit(
            process_user_batch_signals, db, user_id, data["profile"], data["emails"]
        )] = user_id

    for future in as_completed(futures):
        uid = futures[future]
        try:
            processed, drafts = future.result()
        except Exception as e:
            logger.exception(f"Pipeline failed for user {uid[:8]}: {e}")
```

**Consideration:** The Supabase Python client uses httpx under the hood, which is thread-safe. However, verify that the `SupabaseWorkerClient` instance doesn't hold mutable state that could conflict across threads. If it does, create a client per thread.

**Deferral rationale:** With 1-2 users at launch, sequential is fine. Implement before scaling past ~5 active users.

**Files:** `worker/main.py:337-352`

---

### 2.2 Subscription check fails open

**Problem:** `is_subscription_active()` in `worker/supabase_client.py:28-56` returns `True` on any exception. If Supabase is down, all users appear active → uncontrolled API spend.

**Fix:** Add a consecutive-failure counter. After 3 consecutive failures, switch to fail-closed.

```python
_sub_check_failures = 0
_SUB_CHECK_FAIL_THRESHOLD = 3

def is_subscription_active(self, user_id):
    global _sub_check_failures
    try:
        # ... existing logic ...
        _sub_check_failures = 0  # reset on success
        return result
    except Exception as e:
        _sub_check_failures += 1
        if _sub_check_failures >= _SUB_CHECK_FAIL_THRESHOLD:
            logger.error(f"Subscription check failing consistently, blocking processing")
            return False
        logger.warning(f"Subscription check failed, fail-open: {e}")
        return True
```

**Files:** `worker/supabase_client.py:28-56`

---

### 2.3 Mid-pipeline crash recovery

**Problem:** In `worker/run_pipeline.py`, emails are marked "processed" (line ~964) immediately after classification is written, but before response_events and drafts are written. A crash after line 964 but before line 1020 leaves emails marked "processed" with incomplete signal data.

**Fix:** Defer the status update to "processed" until all downstream writes (classification + response_events + draft) succeed for each email. Move the `update_email_status("processed")` call to after the full pipeline for that email completes.

**Files:** `worker/run_pipeline.py:960-970`

---

### 2.4 Onboarding concurrency lock

**Problem:** `worker/main.py:262-269` — if two Railway instances run simultaneously, both can start onboarding for the same user.

**Fix:** Use an atomic status transition. Before starting onboarding, attempt:

```python
result = db.client.table("profiles").update(
    {"onboarding_status": "collecting"}
).eq("id", uid).eq("onboarding_status", "pending").execute()

if not result.data:
    continue  # Another instance already claimed this user
```

This uses the `pending` → `collecting` transition as an implicit lock. Only one instance can succeed because the `.eq("onboarding_status", "pending")` acts as a compare-and-swap.

**Files:** `worker/main.py:262-269`, `worker/onboarding/` (wherever status transitions happen)

---

### 2.5 Missing indexes

**Problem:** Several columns queried frequently lack indexes. Not a problem today but will slow down as data grows.

**Fix:** New migration:

```sql
CREATE INDEX IF NOT EXISTS idx_classifications_user_id ON classifications(user_id);
CREATE INDEX IF NOT EXISTS idx_response_events_user_id ON response_events(user_id);
CREATE INDEX IF NOT EXISTS idx_response_events_sender ON response_events(sender_email);
```

`domain_tiers(user_id, domain)` is already covered by the composite PK from migration 021.

**Files:** New migration `024_add_missing_indexes.sql`

---

### 2.6 Environment variable documentation

**Problem:** Required env vars (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`) aren't documented. Worker crashes with a raw KeyError if missing.

**Fix:** Create `worker/.env.example`:

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
ANTHROPIC_API_KEY=your-anthropic-key

# Optional
POLL_INTERVAL=30
BATCH_SIZE=10
MAX_PIPELINE_WORKERS=3
```

**Files:** `worker/.env.example` (new)

---

## Priority 3 — Post-Launch Polish

### 3.1 Data retention policy
- Add TTL-based cleanup for emails older than a configurable threshold
- Archive or prune `response_events` quarterly
- Not urgent at current scale

### 3.2 Anon key deduplication
- `web/js/pages/login.js:10` redeclares the Supabase anon key instead of importing from `supabase-client.js`
- DRY violation, not a security issue

### 3.3 Non-English email handling
- Signal extraction prompts are English-only
- Flag for future if expanding to non-English markets

---

## Implementation Order

1. **1.1** Onboarding threshold fallback (small change, high user impact)
2. **1.2** Migration renaming (housekeeping, no code changes)
3. **1.3** Contact stats RPC (new migration + Python refactor)
4. **1.4** Thread email bulk fetch (Python refactor only)
5. **2.5** Missing indexes (new migration)
6. **2.6** Env var docs (new file)
7. **2.4** Onboarding lock (small Python change)
8. **2.2** Subscription fail-closed (small Python change)
9. **2.3** Crash recovery (pipeline refactor, test carefully)
10. **2.1** Parallel user processing (larger refactor, defer until user count warrants)

## Files to Create
- `supabase/migrations/023_increment_contact_stats_rpc.sql`
- `supabase/migrations/024_add_missing_indexes.sql`
- `worker/.env.example`

## Files to Modify
- `worker/supabase_client.py` — onboarding threshold, contact stats RPC, subscription check
- `worker/run_pipeline.py` — thread email bulk fetch, crash recovery
- `worker/main.py` — onboarding lock, (later) parallel processing
- `supabase/migrations/` — rename 4 duplicate-numbered files
