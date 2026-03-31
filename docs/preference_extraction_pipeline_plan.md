# Plan: Onboarding Count Tracking + Preference Extraction Pipeline

## Issues Addressed
1. **Style guide count**: Dashboard shows `style_extracted_feature_count` (57 features) — need to also persist `style_sample_email_count` (how many emails were sampled)
2. **Behavioral profile count**: No count column exists — add `behavioral_extracted_feature_count` + `behavioral_sample_email_count`
3. **Preference profile NULL**: No generation code exists — build full extraction pipeline per `docs/preference_layer_implementation_plan.md` Step 3
4. **Preference profile count**: Part of #3 — persist `preference_decision_count`

## Migration (new file: `supabase/migrations/032_profile_counts.sql`)

```sql
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS style_sample_email_count integer DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS behavioral_extracted_feature_count integer DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS behavioral_sample_email_count integer DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS preference_decision_count integer DEFAULT NULL;
```

No new columns needed for `preference_profile` / `preference_profiled_at` — already exist from migration 031.

## File Changes

### 1. `worker/onboarding/prompts.py`

**A. Extend `HAIKU_BEHAVIORAL_EXTRACTION_PROMPT` (~line 247)**

Add two fields to **both** the paired and unpaired JSON output schemas:

```json
{
    "email_index": 1,
    "contact_type": "external_vendor",
    "decision_type": "decides",
    "response_completeness": "addresses_all",
    "commitment_pattern": "specific_next_step",
    "scope_behavior": "stays_narrow",
    "contains_decision": true,
    "decision_quote": "Let's go ahead and renew at the current rate — I don't think we need to shop this around."
}
```

Add to field definitions section (~line 274):

```
- contains_decision: Does this email contain a decision moment where the user
  chose a course of action? A decision requires the user to pick between
  alternatives or commit to an action when alternatives existed. Informational
  replies, scheduling, acknowledgments, and forwarding are NOT decisions.
  For unpaired emails (no inbound parent), set to false — a decision requires
  context to identify what alternatives existed.
- decision_quote: If contains_decision is true, the verbatim excerpt (1-2
  sentences max) from the SENT email where the decision is expressed. Extract
  the exact words — do NOT summarize or interpret. If contains_decision is
  false, set to null.
```

**B. Add `SONNET_PREFERENCE_SYNTHESIS_PROMPT` (new constant)**

Full prompt text inlined below. This is the most complex prompt in the pipeline — all classification options, category definitions, threshold rules, and the observation bias note are load-bearing.

```python
SONNET_PREFERENCE_SYNTHESIS_PROMPT = """\
You are analyzing a user's decision-making patterns extracted from their sent \
emails. Your task is to classify each decision along two personality dimensions \
and then synthesize a categorical profile.

## The Two Traits

### Investment Orientation
Core question: When something is suboptimal, broken, risky, or could be \
improved — does this user invest to address it?

### Positional Stance
Core question: When this user's interests intersect with another party's — or \
when an expert recommends a course of action — does the user yield or advance?

## Step 1: Per-Decision Classification

For each decision moment, classify the signal it provides on each trait.

Investment Orientation — classify as one of:
- "active": The user invested — shopped alternatives, closed a gap, fixed \
fully, investigated root cause, acted preemptively, explored options
- "selective": The user invested, but only after weighing cost-benefit or \
confirming the stakes justified it — qualified investment, conditional commitment
- "conservative": The user chose NOT to invest — accepted the status quo, \
took the partial fix, skipped the optional protection, waited rather than acted
- "no_signal": This decision does not provide evidence on investment \
orientation (e.g., a purely positional negotiation move)

Positional Stance — classify as one of:
- "advancing": The user pushed — negotiated, demanded reciprocity, exploited \
leverage, escalated, challenged the recommended path
- "measured": The user pushed, but selectively — held ground on the key issue \
while accommodating on lesser points, evaluated before following guidance
- "yielding": The user accommodated — conceded, followed expert guidance \
without evaluation, walked away rather than escalated, accepted the proposed terms
- "no_signal": This decision does not provide evidence on positional stance \
(e.g., a purely investment-oriented decision with no counterparty or expert)

IMPORTANT: "no_signal" means this decision provides NO evidence for that \
trait. It is NOT a middle ground or moderate position. Exclude no_signal \
decisions from the count for that trait.

## Step 2: Category Synthesis

After classifying all decisions, assign a category for each trait based on \
the pattern across the full decision set.

There is no middle option. Pick a side.

### Investment Orientation Categories

INVEST HEAVY: Dominant pattern is "active" signals across multiple decision \
types, including low-stakes decisions where most people would accept \
good-enough. Few or no "conservative" signals.

INVEST LIGHT: Majority of signals are "active" or "selective." The user \
invests on important decisions but exercises judgment on lower-stakes items. \
Distinguished from Invest Heavy by the presence of "selective" or \
"conservative" signals on lower-stakes decisions.

CONSERVE LIGHT: Majority of signals are "conservative." The user defaults to \
the status quo but invests when urgency or obvious cost of inaction demands \
it. Distinguished from Conserve Heavy by the presence of "active" signals on \
high-stakes decisions.

CONSERVE HEAVY: Dominant pattern is "conservative" signals across multiple \
decision types, including situations where investing would clearly be the \
better play. Few or no "active" signals.

### Positional Stance Categories

ADVANCE HEAVY: Dominant pattern is "advancing" signals across multiple \
interaction types, including low-stakes situations. Few or no "yielding" signals.

ADVANCE LIGHT: Majority of signals are "advancing" or "measured." The user \
pushes on significant matters but accommodates on lesser points. Distinguished \
from Advance Heavy by the presence of "measured" or "yielding" signals on \
lower-stakes interactions.

YIELD LIGHT: Majority of signals are "yielding." The user defaults to \
accommodation but holds ground when the cost of yielding is obvious. \
Distinguished from Yield Heavy by the presence of "advancing" signals on \
high-stakes matters.

YIELD HEAVY: Dominant pattern is "yielding" signals across multiple \
interaction types, including situations where pushing back would clearly be \
beneficial. Few or no "advancing" signals.

OBSERVATION BIAS NOTE:
Sent emails are biased toward action — decisions where the user chose NOT to \
act often don't generate a sent email. Ask: "what is this user's default when \
they have a genuine choice?" not "what is the ratio of active to conservative \
in the data?"

## Rules
- A single decision may provide signal for one or both traits.
- Some decisions may be ambiguous — classify the stronger signal only, set \
the weaker to no_signal.
- If a trait has fewer than 8 supporting decisions (excluding no_signal), \
return null for that trait.
- If a trait has 8-14 supporting decisions, set confidence to "low" and \
append to the description: "Based on limited data ({N} decisions). This \
profile may shift as more email history becomes available."
- If a trait has 15+ supporting decisions, set confidence to "high".
- Write descriptions in the voice of an observer describing a person, not as \
rules or instructions.
- The description should capture HOW this user reasons about decisions — do \
they lead with cost-benefit analysis, relationship impact, risk mitigation, \
or speed? This reasoning lens emerges from the decision quotes and helps the \
draft model match the justification, not just the direction.

## Output Format (JSON)

Start your response with {{ and end with }}.

{{
  "classifications": [
    {{
      "decision_index": 1,
      "decision_quote": "...",
      "investment_signal": {{"direction": "active|selective|conservative|no_signal", "reasoning": "..."}},
      "positional_signal": {{"direction": "advancing|measured|yielding|no_signal", "reasoning": "..."}}
    }}
  ],
  "investment_orientation": {{
    "category": "invest_heavy|invest_light|conserve_light|conserve_heavy",
    "description": "...",
    "confidence": "high|low",
    "supporting_decisions": 14
  }},
  "positional_stance": {{
    "category": "advance_heavy|advance_light|yield_light|yield_heavy",
    "description": "...",
    "confidence": "high|low",
    "supporting_decisions": 8
  }}
}}

If a trait has fewer than 8 supporting decisions (excluding no_signal), set \
that trait's object to null instead.

## Contact Context
{contact_context}

## Decision Moments
{decisions_json}
"""
```

Note: Double braces `{{` in the prompt are Python f-string / `.format()` escapes — they render as literal `{` in the final prompt. Only `{contact_context}` and `{decisions_json}` are format placeholders.

### 2. `worker/onboarding/extraction.py`

**Modify `extract_behavioral_features()` (~line 239)**

Key design decision: `received_at` comes from the **sent email object's `received_time`** field (when the user sent their reply = when the decision was made), NOT from Haiku's response. Haiku shouldn't pass through metadata it doesn't need for classification.

Implementation:
1. `_prepare_behavioral_batches()` uses a global `pair_index` counter (1 to N across all batches). Haiku echoes this back as `email_index`. So `sampled[email_index - 1]` gives the original email directly — no batch offset math needed.
2. Initialize `decision_moments = []` alongside `all_features`
3. When processing Haiku batch results, for each item with `contains_decision=true` and `decision_quote` present:
   - Bounds-check: `1 <= email_index <= len(sampled)` — skip + log warning if out of range
   - Look up `sampled[email_index - 1]["received_time"]` for the sent timestamp
   - Append `{decision_quote, contact_type, received_at}` to `decision_moments`
4. Add `decision_moments` to return dict

No changes to: `sample_unified_sent_emails()`, `_prepare_behavioral_batches()`, batch size, error handling. Decision fields are optional — if Haiku omits them, the email is simply not a decision moment.

### 3. `worker/onboarding/synthesis.py`

**Add `synthesize_preferences()` function**

```python
MAX_DECISION_MOMENTS = 50

def synthesize_preferences(decision_moments, contact_profiles):
    """Phase 4C-4: Classify decision moments and synthesize preference profile.

    Args:
        decision_moments: list of {decision_quote, contact_type, received_at}
        contact_profiles: enriched contact profiles from Phase 4A

    Returns:
        (preference_profile: dict or None, usage: dict)
    """
```

- Minimum gate: if `len(decision_moments) < 8`, return `(None, {})` without calling Sonnet — mathematically impossible to reach the 8-signal per-trait threshold
- Sort `decision_moments` by `received_at`, cap at 50 most recent
- Format contact context for high-significance contacts (duplicated from behavioral synthesis — intentional per implementation plan to avoid modifying working code path)
- Number decisions as `decision_index: 1..N`
- Call Sonnet with `SONNET_PREFERENCE_SYNTHESIS_PROMPT.format(contact_context=..., decisions_json=...)`
- Parse JSON response via existing `_clean_synthesis_output()` + `json.loads()`
- Validate categories:
  - `valid_io = {"invest_heavy", "invest_light", "conserve_light", "conserve_heavy"}`
  - `valid_ps = {"advance_heavy", "advance_light", "yield_light", "yield_heavy"}`
  - Invalid category → set that trait to None
- If both traits are None → return `(None, usage)`
- Otherwise return `(profile_dict, usage)` where profile_dict has `investment_orientation` and `positional_stance` keys

### 4. `worker/onboarding/runner.py`

**A. Extract decision_moments after Haiku results (~line 227)**
```python
decision_moments = behavioral_result.get("decision_moments", []) if behavioral_result else []
```

**B. Add preference synthesis — decoupled from `skip_guides`**

`skip_guides` branch (~line 287):
```python
if skip_guides:
    topic_result, topic_usage = synthesize_topics(...)
    _merge_usage(sonnet_usage, topic_usage)
    style_guide = None
    behavioral_profile = None
    # Preference runs even when guides are skipped
    preference_profile, preference_usage = synthesize_preferences(
        decision_moments, contact_profiles
    )
    _merge_usage(sonnet_usage, preference_usage)
```

`else` branch (~line 295): add 4th future:
```python
with ThreadPoolExecutor(max_workers=4) as executor:  # was 3
    f_topics = executor.submit(synthesize_topics, ...)
    f_guide = executor.submit(synthesize_style_guide, ...)
    f_behavioral = executor.submit(synthesize_behavioral_profile, ...)
    f_preferences = executor.submit(synthesize_preferences, decision_moments, contact_profiles)
    # ... await all four
```

**C. Persist all counts (~lines 358-374)**
- Style: `db.update_writing_style(user_id, style_guide, extracted_count, sampled_count=sampled_count)`
- Behavioral: `db.update_behavioral_profile(user_id, behavioral_profile, extracted_count=beh_extracted_count, sampled_count=beh_sampled_count)`
- Preference (new block after behavioral):
  ```python
  if preference_profile:
      decision_count = len(decision_moments)
      db.update_preference_profile(user_id, preference_profile, decision_count)
  ```

**D. Do NOT add preference to `missing_components`** — preference is additive, not required

### 5. `worker/supabase_client.py`

**A. Update `update_writing_style()` (~line 553)**
- Add `sampled_count=None` parameter (backwards-compatible default)
- Persist to `style_sample_email_count` when provided

```python
def update_writing_style(self, user_id, style_guide, extracted_count, sampled_count=None):
    data = {
        "writing_style_guide": style_guide,
        "style_profiled_at": datetime.utcnow().isoformat(),
        "style_extracted_feature_count": extracted_count,
    }
    if sampled_count is not None:
        data["style_sample_email_count"] = sampled_count
    self.client.table("profiles").update(data).eq("id", user_id).execute()
```

**B. Update `update_behavioral_profile()` (~line 567)**
- Add `extracted_count=None` and `sampled_count=None` parameters (backwards-compatible defaults)
- Persist `behavioral_extracted_feature_count` and `behavioral_sample_email_count` when provided

```python
def update_behavioral_profile(self, user_id, profile_text, extracted_count=None, sampled_count=None):
    data = {
        "behavioral_profile": profile_text,
        "behavioral_profiled_at": datetime.utcnow().isoformat(),
    }
    if extracted_count is not None:
        data["behavioral_extracted_feature_count"] = extracted_count
    if sampled_count is not None:
        data["behavioral_sample_email_count"] = sampled_count
    self.client.table("profiles").update(data).eq("id", user_id).execute()
```

**C. Add `update_preference_profile()` method**
```python
def update_preference_profile(self, user_id, preference_profile, decision_count=None):
    data = {
        "preference_profile": json.dumps(preference_profile),
        "preference_profiled_at": datetime.utcnow().isoformat(),
    }
    if decision_count is not None:
        data["preference_decision_count"] = decision_count
    self.client.table("profiles").update(data).eq("id", user_id).execute()
```

### 6. `web/js/devtools/onboarding.js`

**Update profile query (~line 16)**
Add to SELECT: `behavioral_profile, behavioral_extracted_feature_count, behavioral_sample_email_count, style_sample_email_count, preference_profile, preference_decision_count`

**Update style guide display (~line 77)**
```
Emails Sampled: ${profile.style_sample_email_count || 0} | Features Extracted: ${profile.style_extracted_feature_count || 0}
```

**Add behavioral profile section**
New section between style guide and scoring, showing:
- Behavioral profile text (same format as style guide display)
- `Emails Sampled: N | Features Extracted: N`

**Add preference profile section**
Display each trait with its category and decision count. Handle partial profiles:
```
Investment: invest_light (14 decisions) | Positional: insufficient data
```
When a trait is null, show "insufficient data" rather than omitting — this helps diagnose whether users produce enough positional decisions or whether the 8-signal threshold is too high.

## Verification

1. **Run migration**: `supabase db push`
2. **Happy path** — Re-run onboarding on test user with >30 sent emails:
   - `style_sample_email_count` is populated (should be ~120 or total sent if < 120)
   - `style_extracted_feature_count` is populated (existing, should stay same)
   - `behavioral_extracted_feature_count` and `behavioral_sample_email_count` populated
   - `preference_profile` is populated with valid JSON (investment_orientation + positional_stance)
   - `preference_decision_count` reflects number of decisions fed to Sonnet
3. **Skip_guides negative test** — Run onboarding on a test user with ~20 sent emails (skip_guides=true):
   - `style_guide` and `behavioral_profile` are null (expected — below 30 threshold)
   - `preference_profile` is still populated if enough decision moments exist (verifies skip_guides decoupling)
   - If <8 decisions for a trait, that trait is null in the profile (partial profile)
4. **Dashboard** — All counts display correctly, partial preference profiles show "insufficient data" for null traits
5. **Draft differentiation** — Run `testing/run_preference_test.py` configs 1-3 to verify drafts still respond to preference profiles correctly
6. **No regression** — Existing style + behavioral synthesis outputs are identical (decision fields are additive, don't change existing Haiku extraction fields)
