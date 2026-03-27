# Round 3 Draft Drop — Root Cause Analysis

**Date:** 2026-03-27
**Severity:** P0 — Silent data loss in draft generation with structural monitoring blind spot
**Symptom:** 2 of 5 test emails classified correctly but received no draft response

---

## Summary

5 test emails were sent to Clarion AI. All 5 were synced, claimed, classified with `needs_response=true`, and marked `processed`. However, only 3 received AI-generated draft responses. The 2 missing drafts were silently dropped — no error logs, no error_message on the pipeline run, no user-visible indication.

The `drafts` table has **zero rows** for the 2 affected emails, confirming `insert_draft()` was never called. The Sonnet API calls failed at both the direct API level and the per-email fallback level, but all exceptions were caught and swallowed — the pipeline completed normally with `status=completed`.

---

## Timeline (all times UTC, 2026-03-27)

### Email Receipt
| Email | received_time | created_at |
|---|---|---|
| Orion Health Systems (Downgrade) | 15:42:06 | 15:42:15 |
| Relocation Package Dispute | 15:42:28 | 15:43:00 |
| Chen v. Broadmark Financial | 15:43:03 | 15:43:46 |
| 1450 Briarwood (Backup Offer) | 15:43:41 | 15:43:46 |
| Payment Processing Outage | 15:44:10 | 15:44:30 |

### Pipeline Runs (Round 3 processing)
| started_at | scanned | processed | drafts | finished_at | duration |
|---|---|---|---|---|---|
| **15:43:16** | **2** | **2** | **0** | **15:43:34** | **18s** |
| 15:44:21 | 2 | 2 | 2 | 15:45:16 | 55s |
| 15:46:02 | 1 | 1 | 1 | 15:46:31 | 29s |

### Classification Timestamps
| Email | classified_at | Has Draft? |
|---|---|---|
| Orion Health Systems | 15:43:21 | NO |
| Relocation Package | 15:43:22 | NO |
| Chen v. Broadmark | 15:44:24 | YES |
| 1450 Briarwood | 15:44:25 | YES |
| Payment Processing | 15:46:05 | YES |

### Drafts Table Verification
| Email | draft_id | has_body | draft_created |
|---|---|---|---|
| Orion Health Systems | **NULL** | **false** | **NULL** |
| Relocation Package | **NULL** | **false** | **NULL** |
| Chen v. Broadmark | e38f45dc... | true | 15:45:15 |
| 1450 Briarwood | 8c089ff4... | true | 15:45:16 |
| Payment Processing | (exists) | true | — |

**No rows exist in the `drafts` table for the 2 missing emails.** `insert_draft()` was never called. `_validate_output()` was never reached. The failure occurred before any draft content was generated or persisted.

---

## Root Cause

The 5 emails arrived in a ~2-minute stagger and were split across 3 pipeline runs. **The first run (15:43:16)** picked up Orion Health and Relocation Package. It classified both correctly (`needs_response=true`, `priority=2`) but generated **0 drafts** and completed in only **18 seconds**.

### What happened in the failing run

The draft generation code in `run_pipeline.py` (lines 1139-1224) has a two-layer API call structure, both with exception handling that silently swallows failures:

**Layer 1 — Direct API path** (`api_client.py:197-225`):
With only 2 requests, `submit_and_wait()` routes to `_submit_direct()` (threshold is 5, set by `DIRECT_API_THRESHOLD`). This makes sequential `client.messages.create()` calls. On `anthropic.APIError`, it sets `results[custom_id] = None` and continues to the next request (line 219-221). It logs a warning but does not raise.

**Layer 2 — Per-email fallback** (`run_pipeline.py:1190-1193`, `drafts.py:240-265`):
For each candidate where `draft_results.get(db_id)` is falsy, the pipeline calls `generate_draft()` as a sync fallback. This method has its own internal try-except (line 262-265) that catches ALL exceptions and returns `(None, {}, None)`. It logs the error but does not raise.

**Result flow** (`run_pipeline.py:1202-1204`):
After both layers fail, `cleaned = None`. The `if cleaned:` check at line 1202 is False, so `insert_draft()` is never called and `drafts_generated` stays at 0. The pipeline reaches its normal completion path at line 1219-1224 with `status=completed`.

### Hypotheses ruled out

| Hypothesis | Evidence | Verdict |
|---|---|---|
| `_is_recent()` rejected old emails | `DRAFT_MAX_AGE_HOURS` not set on Railway → defaults to 24h. Emails were minutes old. | **Ruled out** |
| `enable_draft_generation` was False | Hardcoded to `True` at `run_pipeline.py:67` | **Ruled out** |
| `draft_candidates` was empty due to DB re-query filter | Data flow is entirely in-memory: `filtered` → `email_context` → `draft_candidates`. No DB re-query between classification and draft building. | **Ruled out** |
| `_apply_contact_overrides` set `draft=False` | Only triggers for contacts with `draft_preference="never"`. New test senders have no contact records. | **Ruled out** |
| Top-level exception handler (line 1227) fired | Pipeline run shows `status=completed`, not `status=failed`. Line 1227 sets `status=failed` and marks emails as `error`. Neither happened. | **Ruled out** |
| `_fallback_signals` returned `draft=False` | DB shows `needs_response=true` for both emails — signals were parsed successfully. | **Ruled out** |

### The definitive failure sequence

1. Stage 3 (signal extraction) ran successfully — Haiku classified both emails with `needs_response=true` (~5s)
2. Stage 4 (post-processing) built `draft_candidates` with 2 entries — all gating conditions passed
3. `submit_and_wait()` routed to `_submit_direct()` (2 requests < DIRECT_API_THRESHOLD of 5)
4. `_submit_direct()` called `client.messages.create()` for each — **both failed** with `anthropic.APIError`
   - Each failure logged as warning: `"Direct API call failed for {custom_id}: {e}"`
   - `results[db_id] = None` for both
   - Logged: `"Direct API: 0/2 succeeded"`
5. Back in `run_pipeline.py`, `draft_results` = `{db_id_1: None, db_id_2: None}`
6. `draft_usage` = `{all zeros}` → `record_token_usage` early-returns (sum is 0) → **no token_usage record for the failed calls**
7. Per-email loop: `draft_results.get(db_id)` returns `None` → enters fallback
8. `generate_draft()` called for each — **both failed again** (same transient API issue)
   - Each failure logged as error: `"Draft generation API call failed for '{subject}': {e}"`
   - Returns `(None, {}, None)` — does not raise
9. `fallback_usage = {}` → falsy → no usage recording
10. `cleaned = None` → `if cleaned:` False → `insert_draft()` never called
11. Pipeline completed normally at line 1219: `status=completed`, `emails_processed=2`, `drafts_generated=0`

### Timing analysis

The **18-second runtime** confirms this sequence. A normal 2-draft run takes ~55 seconds (see the 15:44:21 run). The 18 seconds accounts for:
- Signal extraction via Haiku (~8-10s for 2 emails via `_submit_direct`)
- 4 failed Sonnet API calls that returned errors fast (~2-4s each, or possibly connection/rate limit errors returning immediately)
- DB writes for classification results

If the Sonnet calls had succeeded, the run would have taken 40-55 seconds. The fast completion is consistent with API errors returning immediately.

### Token usage verification

The `token_usage` table for 2026-03-27 shows:

| model | stage | request_count | created_at | updated_at |
|---|---|---|---|---|
| haiku | signals | 8 | 15:08:25 | 17:26:40 |
| sonnet | draft | 20 | 15:09:28 | 17:28:24 |

`record_token_usage()` early-returns when all token counts are zero (`supabase_client.py:863-864`). Failed API calls produce zero tokens → never recorded. The 20 sonnet/draft requests represent only **successful** calls. The failed calls from the 15:43:16 run are completely invisible in this table.

---

## Silent Failure Analysis

The failure is invisible at every layer:

1. **No error_logs entries** — The pipeline doesn't write to `error_logs` for draft generation failures. Both `_submit_direct()` and `generate_draft()` use `logger.warning`/`logger.error`, which go to Railway stdout logs only — not persisted to the database.

2. **No error_message on pipeline_run** — The run shows `status=completed`, `error_message=NULL`. The top-level exception handler (line 1227) was NOT triggered — the pipeline completed normally through line 1219. The dual-layer exception handling inside the draft code swallowed all errors before they could reach the top-level handler.

3. **`emails_processed=2` is misleading** — This counter increments at classification time (line 1023), BEFORE draft generation. It reports 2 emails "processed" when neither received a draft. Any monitoring logic that treats `emails_processed > 0` as "fully handled" is structurally blind to draft-stage failures.

4. **`drafts_generated=0` is the only signal** — But nothing in the system alerts on `drafts_generated < emails_processed` when `emails_processed > 0`.

5. **Token usage table is blind** — Failed API calls produce zero tokens and are not recorded. There is no `failed_request_count` metric.

6. **Railway logs are the only place the errors were recorded** — `_submit_direct()` logs `"Direct API call failed for {custom_id}: {e}"` and `generate_draft()` logs `"Draft generation API call failed for '{subject}': {e}"`. These are ephemeral stdout logs with limited retention.

---

## Recommended Fixes

### P0 — Must Fix

1. **Mark pipeline runs as `partial_failure`** when `drafts_generated < len(draft_candidates)`. The pipeline currently reports `status=completed` even when all draft generation fails. Add a status check after the draft loop:
   ```python
   status = "completed"
   if draft_candidates and drafts_generated < len(draft_candidates):
       status = "partial_failure"
   ```
   The pipeline should not lie about its own outcome.

2. **Split `emails_processed` into `emails_classified` and `emails_drafted`**. The current counter increments at classification (line 1023) but is named and treated as if it means "fully processed." This makes the monitoring system structurally unable to detect this class of failure. Until this is fixed, no amount of alerting logic can distinguish a successful run from one that silently dropped all drafts.

3. **Add per-draft failure logging to the database**. The existing `logger.warning` and `logger.error` calls in `_submit_direct()` and `generate_draft()` only go to stdout. Add `error_logs` table inserts (or `error_message` on the pipeline_run) so draft failures are visible in the dashboard. Specific additions:
   - When `_submit_direct()` returns `None` for a request, log the error to `error_logs`
   - When `generate_draft()` fallback also fails, log to `error_logs`
   - When the draft loop completes with `drafts_generated < len(draft_candidates)`, set `error_message` on the pipeline_run with the list of failed email subjects

4. **Wrap the per-email draft loop body** (lines 1186-1217) in its own try-except. Currently, if any unexpected exception escapes (not caught by the two inner layers), it propagates to the top-level handler at line 1227 which marks ALL emails as `error` and returns `(0, 0)`. The per-email try-except is a safety net against unforeseen failure modes:
   ```python
   for candidate in draft_candidates:
       try:
           # ... existing draft logic ...
       except Exception as e:
           logger.error(f"  Draft failed for {candidate['db_id'][:8]}: {e}")
           # continue to next candidate
   ```

### P1 — Should Fix

5. **Add a draft backfill job**. A periodic job that finds emails with `needs_response=true` classification but no corresponding draft row, and retries draft generation with a delay. This is the safety net for every draft failure mode — not just this specific one. Immediate retry in the same run is likely to hit the same transient issue (rate limit, API outage). The better pattern: finish the run with `partial_failure` status, let the backfill job pick it up on the next cycle.

6. **Dashboard alert for partial failures**. Surface pipeline runs where `drafts_generated < emails_classified` (once the counter split from P0 #2 is in place) as warnings in the Clarion AI dashboard.

### P2 — Nice to Have

7. **Tune accumulation window for efficiency**. The staggered arrival caused the 5 emails to fragment across 3 runs, but this did not cause the draft failure (the other 2 runs with equally small batches of 2 and 1 emails succeeded). Tuning the window is a throughput optimization, not a reliability fix.

---

## Data Verification Queries

```sql
-- Confirm no draft rows exist for the 2 missing emails
SELECT e.subject, d.id as draft_id, d.draft_body IS NOT NULL as has_body, d.created_at as draft_created
FROM emails e
LEFT JOIN drafts d ON d.email_id = e.id
WHERE e.received_time >= '2026-03-27 15:40:00'
ORDER BY e.received_time ASC;

-- Emails with classification but no draft
SELECT e.subject, c.needs_response, c.priority, d.id as draft_id
FROM emails e
JOIN classifications c ON c.email_id = e.id
LEFT JOIN drafts d ON d.email_id = e.id
WHERE e.received_time >= '2026-03-27 15:40:00'
ORDER BY e.received_time;

-- Pipeline runs showing the 3-way split and timing
SELECT id, emails_scanned, emails_processed, drafts_generated,
       error_message, started_at, finished_at,
       finished_at - started_at as duration
FROM pipeline_runs
WHERE user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
AND started_at >= '2026-03-27 15:43:00'
ORDER BY started_at ASC;

-- Token usage for the day (failed API calls are NOT recorded here)
SELECT model, stage, request_count, input_tokens, output_tokens, created_at, updated_at
FROM token_usage
WHERE user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
AND usage_date = '2026-03-27'
ORDER BY created_at ASC;
```
