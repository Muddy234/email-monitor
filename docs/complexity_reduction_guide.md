# Complexity Reduction Guide

Four areas where current complexity outpaces the value delivered. Each section explains the problem, what to keep, what to remove, and the specific code changes.

---

## Area 1: Model Trainer Simplification

**File:** `worker/onboarding/model_trainer.py`

### Problem

The model trainer runs 6 steps on 50–200 response events:

1. Feature importance analysis (boolean lifts, msg_type, recipient bins, depth bins, rate×TO interaction)
2. Lift factor derivation with dampening
3. Recurring pattern detection (CV-based cadence matching)
4. Score all events using derived lifts
5. PAVA isotonic regression calibration
6. Triage threshold computation

With <200 events, most of these produce noisy estimates. Meanwhile the scorer (`pipeline/scorer.py`) hard-codes the penalties that actually matter:

| Hard-coded in scorer | Value |
|---------------------|-------|
| CC-only penalty (high sender) | 0.9 |
| CC-only penalty (low sender) | 0.8 |
| Cold-start dampening (<3 emails) | 0.7 |
| Low thread participation (<5%) | 0.5 |
| Med thread participation (<15%) | 0.75 |
| Thread recency <24h | 1.5 |
| Thread recency <72h | 1.2 |

### What to keep

- **global_rate** — global response rate, used as Bayesian prior everywhere
- **boolean_lifts** — per-feature lift factors (only features with >3% separation, `SEPARATION_THRESHOLD`). These are the highest-signal trained parameters: `user_in_to`, `user_sole_to`, `mentions_user_name`, `has_question`, `has_action_language`, `sender_is_internal`, `thread_user_initiated`, `arrived_during_active_hours`, `arrived_on_active_day`
- **msg_type_lifts** — lift per message type (`new`, `reply`, `forward`). Simple, stable signal
- **prior_weight** — Bayesian smoothing constant (currently 3)
- **DEFAULT_PARAMETERS** fallback — still needed for <50 events
- **check_retrain_needed()** — still useful

### What to remove

| Component | Why remove | Lines |
|-----------|-----------|-------|
| `recipient_bins` | Scorer hard-codes CC penalty; binning 5 recipient ranges from <200 events is noise | `_analyze_features` L232-242, `_derive_lift_factors` L337-344 |
| `depth_bins` | Scorer hard-codes thread participation penalties; depth bins overlap with those | `_analyze_features` L245-260, `_derive_lift_factors` L347-360 |
| `rate_x_to` interaction | 4-tier matrix cross-cutting rate bins × TO position from <200 events. The `user_in_to` boolean lift already captures the TO signal | `_analyze_features` L263-289, `_derive_lift_factors` L362-388, `_score_all_events` L474-480 |
| `_fit_isotonic` (PAVA) | Isotonic regression on sparse data overfits — creates jagged calibration that doesn't generalize. Use raw probabilities directly | `_fit_isotonic` L566-609, `_isotonic_transform` L612-619 |
| `recurring_patterns` | Duplicates the `is_recurring` boolean lift already computed in stats_extraction. The pattern→rate lookup adds complexity for marginal gain | `_detect_recurring_patterns` L395-433, `_score_all_events` L451-454 |
| `_compute_triage` | Computed from isotonic output. Use fixed thresholds instead | L626-661 |

### Simplified train_user_model()

```python
def train_user_model(db, user_id):
    events = db.fetch_response_events(user_id)
    if len(events) < MIN_EVENTS_TO_TRAIN:
        # ... existing default fallback ...
        return params

    total_responded = sum(1 for e in events if e.get("responded"))
    global_rate = total_responded / len(events)

    # Step 1: Boolean lifts (only features with meaningful separation)
    boolean_lifts = _compute_boolean_lifts(events, global_rate)

    # Step 2: Message type lifts
    msg_type_lifts = _compute_msg_type_lifts(events, global_rate)

    params = {
        "meta": {
            "user_id": user_id,
            "total_events": len(events),
            "global_rate": round(global_rate, 4),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": 2,  # v2 = simplified model (boolean + msg_type only)
        },
        "prior_weight": BAYESIAN_PRIOR_WEIGHT,
        "lift_factors": {
            "boolean": boolean_lifts,
            "msg_type": msg_type_lifts,
            "recipient_bins": {},   # Intentionally empty in v2
            "depth_bins": {},       # Intentionally empty in v2
            "rate_x_to": {},        # Intentionally empty in v2
        },
        "iso_breakpoints": [],      # Intentionally empty in v2
        "recurring_patterns": {},   # Intentionally empty in v2
        "triage": {"hard_gate_threshold": 0.01, "soft_gate_threshold": 0.03},
        "thresholds": {
            "combined_penalty_floor": COMBINED_PENALTY_FLOOR,
            "score_cap": SCORE_CAP,
            "score_floor": SCORE_FLOOR,
        },
    }

    db.upsert_scoring_parameters(user_id, params, emails_used=len(events))
    return params
```

### Schema versioning

The empty keys (`recipient_bins: {}`, `depth_bins: {}`, etc.) are intentional for backward compatibility — the scorer reads these with `.get()` fallbacks. But without a version marker, there's no way to distinguish "simplified model that intentionally omits these" from "failed training that produced empty results."

The `schema_version: 2` field in meta solves this:
- **v1** (implicit, no field): full model with all components populated
- **v2**: simplified model — boolean lifts + msg_type only, empty keys are by design

This also provides a clean migration path if any component (e.g., isotonic calibration) is re-enabled at higher data volumes — check the version and handle accordingly.

### Triage threshold provenance

The hard-coded thresholds `hard_gate_threshold: 0.01` and `soft_gate_threshold: 0.03` were originally derived by `_compute_triage()` from isotonic-calibrated predictions. With PAVA removed, they become static defaults. These values came from the initial training run where `global_rate ≈ 0.25`:
- `hard_gate_threshold = 0.01` — effectively "never respond to this sender/pattern"
- `soft_gate_threshold = 0.03` — calibrated probability below which cumulative response rate < 5%

At current data volumes, fixed thresholds are fine. If `global_rate` shifts significantly (e.g., a user who responds to 60% of emails), consider tying `soft_gate` to `global_rate` — something like `soft_gate = max(0.03, global_rate * 0.05)` — so the gate scales with the user's baseline.

### Scorer compatibility

The scorer already handles empty/missing values gracefully:
- `recipient_multipliers = {}` → `_lookup_bin()` returns 1.0
- `depth_multipliers = {}` → `_lookup_bin()` returns 1.0
- `rate_x_to_interaction = {}` → `.get(rate_bin)` returns None, skipped
- `iso_breakpoints = []` → `_isotonic_transform()` returns raw score
- `recurring_patterns = {}` → key lookup misses, falls through to sender/global rate

No changes needed to `scorer.py`.

### Functions to delete

- `_detect_recurring_patterns()` (L395-433)
- `_score_all_events()` (L440-506) — only used to feed PAVA
- `_compute_sender_stats()` (L509-529) — only used by `_score_all_events()`
- `_get_rate_tier()` (L532-540) — only used by `_score_all_events()`
- `_lookup_bin()` (L543-559) — only used by `_score_all_events()`. Note: this is the **trainer's** copy; the scorer has its own `_lookup_bin()` at `scorer.py:135-142` which is a separate implementation and remains unchanged.
- `_fit_isotonic()` (L566-609)
- `_isotonic_transform()` (L612-619) — only used by `_compute_triage()`
- `_compute_triage()` (L626-661)

### Functions to simplify

- `_analyze_features()` → rename to `_compute_boolean_lifts()`, remove recipient bins, depth bins, rate×TO
- `_derive_lift_factors()` → inline into `_compute_boolean_lifts()`, remove recipient/depth/rate×TO sections
- Extract `_compute_msg_type_lifts()` from existing msg_type block in `_analyze_features()`

### Net result

~660 lines → ~200 lines. Same scorer output quality at current data volumes.

---

## Area 2: Remove Sonnet Contact Synthesis

**Files:** `worker/onboarding/synthesis.py`, `worker/onboarding/runner.py`

### Problem

`synthesize_contacts()` makes a Sonnet API call to infer `inferred_role`, `expertise_areas`, `relationship_summary` for up to 50 contacts.

**These fields are NOT used by the scorer.** The scorer (`pipeline/scorer.py`) only reads from contacts:
- `response_rate` (line 208)
- `total_received` (line 211)
- `contact_type` (line 252)

The Sonnet-inferred fields are used only in:
- `enrichment.py` sender briefing (lines 70-71, 122) — but all have graceful fallbacks
- DB storage (contacts table columns) — displayed on dashboard but not actionable

### What to keep

- **`synthesize_topics()`** — low token cost, produces domains + keywords
- **`synthesize_style_guide()`** — used by the draft generator (the only Sonnet output that feeds into a downstream system)
- **`_build_stats_only_contacts()`** in `runner.py` — already writes all scorer-relevant fields from pure Python extraction

### What to remove from runner.py

**Phase 4A block (lines 189-218):**
```python
# DELETE: Phase 4A contact profile synthesis
contact_profiles = synthesize_contacts(...)
# ... and fallback logic ...
# ... and enriched contacts DB write ...
```

**Helper functions:**
- `_merge_extraction_into_contacts()` (lines 368-392) — only used to merge Sonnet profiles with stats
- `_fallback_contact_profiles()` (lines 327-343) — fallback for failed Sonnet synthesis

### Enrichment fallback behavior

`enrichment.py:_build_sender_briefing()` already handles missing Sonnet fields:
- Line 70: `role = contact.get("inferred_role", "unknown")` — defaults to "unknown"
- Line 71: `org = contact.get("inferred_organization", domain.split(".")[0].title())` — falls back to domain name
- Lines 122-132: If `relationship_summary` is None, builds summary from stats (rate, latency, frequency)

No changes needed to `enrichment.py` for functional correctness.

### Fast follow: Organization name quality

After removing Sonnet synthesis, **every** contact hits the `domain.split(".")[0].title()` fallback for organization name — not just the ones where Sonnet previously failed. This produces:
- `"Gmail"` for personal gmail.com addresses
- `"Yahoo"` for yahoo.com
- `"Zionsbank"` for zionsbank.com (no space, no proper casing)

When Tyler or Gina show up in the enrichment briefing as "Gmail" instead of "Arete Collective," that's a noticeable regression in what Haiku sees.

**Not a blocker for the removal**, but worth a cheap Python-side improvement as a fast follow:

```python
# In enrichment.py or a shared util
_CONSUMER_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                     "aol.com", "icloud.com", "protonmail.com", "live.com"}

def _infer_org(sender_email, sender_name, domain):
    """Infer organization from domain, with consumer-domain fallback."""
    if domain in _CONSUMER_DOMAINS:
        # Don't say "Gmail" — use sender's display name or just "personal"
        return sender_name if sender_name else "personal"
    # For corporate domains, title-case the domain name
    return domain.split(".")[0].replace("-", " ").replace("_", " ").title()
```

This covers the worst cases (consumer providers) without needing an API call. Internal domain detection (`arete-collective.com`) is already handled by the `sender_is_internal` check in `scorer.py:386`.

### Runner.py Stage 2 after removal

```python
# Stage 2 becomes:
# Phase 3 + 4C-1: Haiku extraction (unchanged)
# Phase 4B: Topic synthesis (unchanged)
# Phase 4C-2: Style guide synthesis (unchanged)
# Phase 4A (contact synthesis) — REMOVED
```

### Cost savings

Removes 1 Sonnet API call per onboarding (~8K tokens). Contact stats come from pure Python extraction at zero marginal cost.

---

## Area 3: Two-Tier Response Matching

**File:** `worker/onboarding/stats_extraction.py`, function `_find_response()` (lines 239-290)

### Problem

Three-tier matching currently:

| Tier | Match criteria | Window | Risk |
|------|---------------|--------|------|
| 1 | conversation_id | 48h | Low — explicit thread link |
| 2 | Subject similarity + recipient + sender | 48h | Low — multiple signals |
| 3 | Sender only | 4h | **High — no subject check** |

Tier 3 matches any sent email to the same person within 4 hours regardless of subject similarity. If user emails person A about topic X, then receives an unrelated email from person A about topic Y within 4 hours, Tier 3 falsely matches them.

### What to change

Remove the Tier 3 block (lines 276-288):

```python
    # DELETE: Tier 3 loose matching
    # Tier 3: Sent to sender within 4h (loose) — closest match
    if received_time and sender and sender in sent_by_recipient:
        best = None
        best_delta = None
        for s in sent_by_recipient[sender]:
            sent_time = _parse_time(s.get("received_time"))
            if sent_time and sent_time > received_time:
                delta = (sent_time - received_time).total_seconds()
                if delta < 4 * 3600 and (best_delta is None or delta < best_delta):
                    best = s
                    best_delta = delta
        if best:
            return best
```

### Impact assessment

- Tier 1 (conversation_id) handles most matches in Outlook/Exchange (which provides conversation_id natively)
- Tier 2 (subject similarity) catches the rest — same subject line, sent to same person, within 48h
- Tier 3 only fires when neither conversation_id nor subject matches, which is likely a false positive anyway
- Net effect: fewer false-positive response labels → cleaner training data for the model


---

## Area 4: Replace ThreadPoolExecutor with Sequential Calls

**File:** `worker/onboarding/runner.py`

### Problem

Two `ThreadPoolExecutor(max_workers=2)` blocks run 4 total function calls in parallel:

**Block 1 (lines 161-180):**
```python
with ThreadPoolExecutor(max_workers=2) as executor:
    f_extract = executor.submit(extract_email_features, received, ...)
    f_style = executor.submit(extract_writing_styles, sent)
    extraction_result = f_extract.result()
    style_result = f_style.result()
```

**Block 2 (lines 226-237):**
```python
with ThreadPoolExecutor(max_workers=2) as executor:
    f_topics = executor.submit(synthesize_topics, ...)
    f_guide = executor.submit(synthesize_style_guide, ...)
    topic_result = f_topics.result()
    style_guide = f_guide.result()
```

On 100 emails, each API call takes 3-10 seconds. Parallel execution saves ~5-10 seconds total but:
- Exceptions in futures have harder-to-read stack traces
- Logging from parallel calls interleaves
- Debugging requires understanding which future failed

### Error handling philosophy

Each Stage 2 call is either **fatal** (abort onboarding) or **recoverable** (continue with degraded output). The classification:

| Call | Fatal? | On failure | Rationale |
|------|--------|-----------|-----------|
| `extract_email_features()` | **Yes** | `return False` | Produces keyword frequencies and per-email extractions that feed topic synthesis and scoring. Without it, nothing downstream has data to work with. |
| `extract_writing_styles()` | No | `style_result = None` | Style guide is only used by draft generator. User still gets scoring and classification without it. |
| `synthesize_topics()` | No | `topic_result = {"domains": [], "high_signal_keywords": []}` | Topic domains are display-only in the user profile. Empty list is a valid (if unhelpful) state. |
| `synthesize_style_guide()` | No | `style_guide = None` | When None, `update_writing_style()` is skipped. Draft generator falls back to generic style prompting. |

### Replacement

**Block 1:**
```python
# Phase 3: Email feature extraction (Haiku) — FATAL on failure
try:
    extraction_result = extract_email_features(received, stats["contact_frequencies"])
except Exception:
    logger.exception("Phase 3: email feature extraction raised")
    extraction_result = None

# Phase 4C-1: Writing style extraction (Haiku) — recoverable
try:
    style_result = extract_writing_styles(sent)
except Exception:
    logger.exception("Phase 4C-1: writing style extraction raised")
    style_result = None

if extraction_result is None:
    logger.error("Phase 3 extraction failed completely")
    db.update_onboarding_status(user_id, "failed")
    return False
```

**Block 2** (after Area 2 removal of `synthesize_contacts`):
```python
# Phase 4B: Topic synthesis (Sonnet) — recoverable (empty fallback)
try:
    topic_result = synthesize_topics(extraction_result.get("keyword_frequencies", {}))
except Exception:
    logger.exception("Phase 4B: topic synthesis raised")
    topic_result = None

# Phase 4C-2: Style guide synthesis (Sonnet) — recoverable (None → no style written)
try:
    style_guide = synthesize_style_guide(
        style_result.get("style_features", []) if style_result else [],
        [],  # No contact_profiles after Area 2 removal
    )
except Exception:
    logger.exception("Phase 4C-2: style guide synthesis raised")
    style_guide = None

if topic_result is None:
    topic_result = {"domains": [], "high_signal_keywords": []}
```

### Cleanup

Remove `from concurrent.futures import ThreadPoolExecutor` from runner.py imports.

---

## Implementation Order

| Step | Area | Risk | Reason for order |
|------|------|------|-----------------|
| 1 | Area 3 (two-tier matching) | Low | Smallest change, isolated to one function |
| 2 | Area 4 (sequential calls) | Low | Mechanical refactor, same behavior |
| 3 | Area 2 (remove contact synthesis) | Medium | Removes an API call; enrichment fallbacks need verification |
| 4 | Area 1 (model trainer) | Medium | Largest change; scorer compatibility already confirmed |

## Verification

After each area:
1. Reset onboarding status, re-run onboarding
2. Watch Railway logs for all stages completing
3. Spot-check: contacts table still has stats fields, scoring still produces reasonable calibrated probabilities

### Area 1 regression test (concrete)

Before deploying the simplified model trainer:
1. Fetch the current `scoring_parameters` JSON for the test user (v1 full model)
2. Run the simplified trainer to produce v2 params
3. Score the same 50 recent emails with both param sets using `score_email()`
4. Compute mean absolute difference in calibrated probabilities: `mean(|cal_v1 - cal_v2|)`
5. **Pass/fail threshold: <5% average deviation.** If exceeded, investigate which removed component was contributing meaningful signal at the current data volume

```python
# Quick script for the regression test
old_params = db.fetch_scoring_parameters(user_id)  # v1
new_params = train_user_model_v2(db, user_id)       # v2

old_artifacts = UserScoringArtifacts(old_params)
new_artifacts = UserScoringArtifacts(new_params)

diffs = []
for email in recent_50_emails:
    _, cal_old, _, _ = score_email(email, signals, contact, thread, old_artifacts)
    _, cal_new, _, _ = score_email(email, signals, contact, thread, new_artifacts)
    diffs.append(abs(cal_old - cal_new))

mad = sum(diffs) / len(diffs)
print(f"Mean absolute deviation: {mad:.4f} ({mad*100:.1f}%)")
assert mad < 0.05, f"Regression threshold exceeded: {mad:.4f}"
```

