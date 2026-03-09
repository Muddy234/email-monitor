# Plan: Fix Scoring Pipeline — Full Feature Training, Continuous Learning, Universal Contacts

## Summary

Three interconnected fixes to make the scoring model use all available signals, retrain over time, and track every sender:

1. **Model trainer uses only 4/17 multipliers** — fix `_analyze_features()` to train on all 10 boolean features + thread depth
2. **No continuous learning** — runtime response_events lack the `responded` label, so retraining (already wired in `main.py:220`) trains on stale data
3. **Contacts limited to top 50** — `_merge_extraction_into_contacts()` only writes Sonnet-profiled contacts; all other senders' stats are discarded

---

## Change 1: DB Migration — Add feature columns to response_events

**New file:** `supabase/migrations/007_response_events_features.sql`

Add 7 columns to `response_events`:
- `mentions_user_name` (boolean, default false)
- `sender_is_internal` (boolean, default false)
- `thread_user_initiated` (boolean, default false)
- `arrived_during_active_hours` (boolean)
- `arrived_on_active_day` (boolean)
- `thread_depth` (integer, default 1)
- `scoring_factors` (text[]) — persist the factors list for observability

---

## Change 2: Fix model_trainer.py — Enable all features

**File:** `worker/onboarding/model_trainer.py`

### 2a. `_analyze_features()` (line 173)
- Expand `bool_features` list to include `thread_user_initiated`, `arrived_during_active_hours`, `arrived_on_active_day`
- Replace hardcoded `False` at lines 183-184 — read stored values from the event row, fall back to `False` if `None`
- **Note:** After adding new features, verify that the feature keys in `_analyze_features()` match what `_derive_lift_factors()` reads from the results dict. A key mismatch would silently produce missing lifts without erroring.

### 2b. `_analyze_features()` — Add depth bin analysis (new block after recipient bins)
- Analyze `thread_depth` column from events using the same bin approach as recipient bins
- Bins: `[1,2)`, `[2,4)`, `[4,8)`, `[8,20)`, `[20,999)`
- Log the actual thread depth distribution during onboarding for future bin tuning

### 2c. `_derive_lift_factors()` (lines 305-309)
- Replace hardcoded `depth_bins = {... 1.0 ...}` with actual lift computation from `feature_results["depth_bins"]`
- Keep identity fallback if no depth data exists (backward compat)

---

## Change 3: Enrich response_events during onboarding extraction

**File:** `worker/onboarding/stats_extraction.py`

### 3a. `_build_response_events()` — Add `mentions_user_name`
- Port name-mention detection from `run_pipeline.py`'s `build_signals()` logic
- Split user alias local parts on `._-`, check body for token matches
- Add `"mentions_user_name": bool` to each event dict

### 3b. `_build_response_events()` — Add `sender_is_internal`
- Derive user domain(s) from aliases
- Check if sender domain matches any user domain
- Add `"sender_is_internal": bool` to each event dict

### 3c. `extract_all()` — Post-enrichment for thread features
- After `_build_threads()` returns, call new `_enrich_thread_features(response_events, threads)`
- Sets `thread_user_initiated` and `thread_depth` on each event from its conversation's thread data

### 3d. `extract_all()` — Post-enrichment for active time features
- After `_build_user_profile()` returns, call new `_enrich_active_time_features(response_events, user_profile)`
- Derives `arrived_during_active_hours` and `arrived_on_active_day` from received_time + user's active hours/days
- Falls back to business hours (8-18 M-F) if no profile data
- **Note:** This fallback should only fire for pre-migration historical events during retraining. At runtime, the pipeline gates behind `onboarding_status = 'complete'` (checked in `main.py` accumulation), so the user profile with real active hours is always available. Pre-onboarding users never enter the scoring pipeline.

---

## Change 4: Write ALL contacts during onboarding

**File:** `worker/onboarding/runner.py`

### 4a. After `_merge_extraction_into_contacts()` at line 161
- Identify senders in `extraction["contacts"]` NOT in the Sonnet-profiled set
- Build stats-only contact records for them (raw stats, no LLM-generated fields)
- Call `db.upsert_contacts()` for the stats-only batch
- Keep synthesis.py top-50 cap for LLM profiling (expensive) — this only affects the LLM-drafted profiles, not raw stats

**Consumer audit for stats-only contacts:** The scorer reads only `response_rate`, `total_received`, `contact_type`, and `smoothed_rate` from contacts — all of which come from extraction stats, not Sonnet. The Sonnet-only fields (`relationship_summary`, `expertise_areas`, `communication_style`, etc.) are only consumed by draft generation and the dashboard. No None dereference risk from stats-only records.

---

## Change 5: Persist new feature columns + scoring_factors

**File:** `worker/supabase_client.py`

### 5a. `upsert_response_events()` (line 596)
- Add 7 new columns to the row dict: `mentions_user_name`, `sender_is_internal`, `thread_user_initiated`, `arrived_during_active_hours`, `arrived_on_active_day`, `thread_depth`, `scoring_factors`

### 5b. New method: `label_response_events_responded(user_id, email_ids)`
- UPDATE response_events SET responded=True WHERE email_id IN (email_ids)
- Used by retroactive response labeling

### 5c. New method: `bulk_upsert_contact_stats(user_id, sender_stats)`
- Batch upsert minimal contact records for senders not yet in contacts table
- Use atomic `ON CONFLICT (user_id, email) DO UPDATE SET total_received = contacts.total_received + excluded.total_received, last_interaction_at = excluded.last_interaction_at` — avoids read-then-write race conditions between overlapping pipeline batches

---

## Change 6: Runtime pipeline — response labeling, contact updates, feature enrichment

**File:** `worker/run_pipeline.py`

### 6a. Retroactive response labeling
- New function `_update_response_labels(db, user_id, threads_map, user_aliases)`
- Scans threads for user-sent messages; any preceding inbound message in that thread gets labeled `responded=True`
- Called once per pipeline batch, after scoring
- **Important:** This label is retrospective — it updates rows that were already scored in previous batches. It does NOT affect the score that was already served for those emails. The label's purpose is purely to improve retraining data quality. The retraining loop picks up the corrected labels on its next cycle.

### 6b. Runtime contact stat updates
- After scoring, identify senders NOT in `contacts_map`
- Call `db.bulk_upsert_contact_stats()` to create/increment contact records
- Ensures every sender has a contact row for future scoring

### 6c. Enrich runtime response_events with new features
- Update the response_events dict at lines 669-686 to include:
  - `mentions_user_name` from `signals.get("user_mentioned_by_name")`
  - `sender_is_internal` from contact/domain check
  - `thread_user_initiated` from `thread_info.get("user_initiated")`
  - `arrived_during_active_hours` / `arrived_on_active_day` derived from received_time + user profile
  - `thread_depth` from `thread_info.get("total_messages")`
  - `scoring_factors` from the `factors` list (for observability)

### 6d. Minor: scorer.py active hours/day
- Replace hardcoded `True` at `scorer.py:247-248` with actual derivation from user profile (passed as param or computed in `_score_and_gate`)

---

## Retraining Trigger

The continuous learning loop is already wired in `main.py:215-224`. It runs `check_retrain_needed()` every main loop cycle for each active user.

**Existing triggers** (`model_trainer.py:137-162`):
- **Time-based:** >30 days since last `generated_at` timestamp
- **Volume-based:** >500 new response_events since last training (via `count_response_events_since()`)

**Constants:**
- `RETRAIN_DAYS_THRESHOLD = 30`
- `RETRAIN_EVENT_THRESHOLD = 500`

**Consideration:** At ~20-50 emails/day, 500 events takes 10-25 days to accumulate. The 30-day timer acts as a backstop. This means the model updates roughly monthly. If faster convergence is desired post-deployment (e.g., to quickly incorporate the newly labeled data), temporarily lower `RETRAIN_EVENT_THRESHOLD` to 200 for the first few cycles, then restore to 500.

---

## Implementation Order

1. Migration (`007_response_events_features.sql`) — safe, adds nullable columns
2. `supabase_client.py` — add new columns + new methods
3. `stats_extraction.py` — add feature extraction + enrichment functions
4. `runner.py` — write all contacts
5. `model_trainer.py` — enable all features + depth bins
6. `run_pipeline.py` + `scorer.py` — runtime enrichment, labeling, contact updates

---

## Verification

1. **Migration**: Run migration, verify columns exist with `\d response_events`
2. **Onboarding re-run**: Re-run onboarding for test user, verify:
   - All contacts written (not just 50) — `SELECT count(*) FROM contacts WHERE user_id = ?`
   - response_events have new columns populated — `SELECT mentions_user_name, sender_is_internal, thread_depth FROM response_events LIMIT 10`
3. **Model retrain**: Trigger retrain, verify scoring_parameters now has lifts for new features:
   - `SELECT parameters->'lift_factors'->'boolean' FROM scoring_parameters WHERE user_id = ?`
   - Depth bins should have non-1.0 values
4. **Runtime pipeline**: Process a few emails, verify:
   - New response_events have all feature columns populated
   - New senders get contact records
   - scoring_factors column persisted
5. **Response labeling**: After user replies to an email, verify the corresponding response_event gets `responded=True`
6. **End-to-end**: Score the morlinsky email scenario again — should produce a higher calibrated_prob due to additional active features

---

## Critical Files

| File | Lines | Change |
|------|-------|--------|
| `supabase/migrations/007_response_events_features.sql` | new | Add 7 columns |
| `worker/onboarding/model_trainer.py` | 173-184, 305-309 | Enable all boolean features, fix depth bins |
| `worker/onboarding/stats_extraction.py` | 44-88, ~170 | Add feature enrichment to events |
| `worker/onboarding/runner.py` | 157-161 | Write all contacts, not just top 50 |
| `worker/run_pipeline.py` | 639-718 | Response labeling, contact updates, feature columns |
| `worker/supabase_client.py` | 592-624 | New columns + 2 new methods (atomic upsert) |
| `worker/pipeline/scorer.py` | 247-248 | Derive active hours/day from profile |
