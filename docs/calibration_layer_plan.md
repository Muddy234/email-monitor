# Calibration Layer Implementation Plan

## Summary

Add a post-onboarding calibration stage that tests the synthesized personality profile against the user's own sent email history, scores the delta across style/behavioral/preference/contextual dimensions, generates specific correction rules from hard misses, and iterates until exit thresholds are met. Drafts are not generated for live incoming emails until calibration passes.

**Pipeline stages after implementation:**

1. **Onboarding** — unchanged. Builds style guide, behavioral profile, preference profile from email history.
2. **Calibration** (new) — tests profile against sent emails, scores results, generates correction rules, re-tests until thresholds pass. Capped at 3 iterations.
3. **Live drafting** — production pipeline with calibrated profile + correction rules injected into every draft prompt.

Calibration is a gate. The system does not generate drafts for real incoming emails until calibration has passed exit criteria.

## Architecture Overview

```
Onboarding complete
        │
        ▼
┌─────────────────────┐
│  Select 15 test     │
│  emails from sent   │
│  history            │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Layer 1: Score      │
│  user's actual sent  │◄── Mechanical + Opus 4.6
│  emails              │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Generate test       │
│  drafts via draft    │◄── Opus 4.6 (production pipeline)
│  pipeline            │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Layer 2: Score      │
│  generated drafts    │◄── Mechanical + Opus 4.6
│  against Layer 1     │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Check exit          │
│  criteria            │
└────────┬────────────┘
         │
    Pass │         Fail (max 3 iterations)
         │              │
         ▼              ▼
┌──────────────┐  ┌─────────────────────┐
│  Mark user   │  │  Generate correction │
│  calibrated  │  │  rules from hard     │
│  → live      │  │  misses → re-test    │
│  drafting    │  └─────────────────────┘
└──────────────┘
```

## Files to Modify / Create

| File | Change |
|------|--------|
| `worker/calibration/__init__.py` | New module. |
| `worker/calibration/email_selector.py` | New. Test email selection logic with stratified sampling. |
| `worker/calibration/ground_truth_scorer.py` | New. Layer 1 scoring — scores the user's actual sent emails against profile dimensions. |
| `worker/calibration/draft_scorer.py` | New. Layer 2 scoring — scores generated drafts against Layer 1 ground truth. |
| `worker/calibration/correction_generator.py` | New. Clusters hard misses, generates correction rules. |
| `worker/calibration/runner.py` | New. Orchestrates the calibration loop: select → score → draft → compare → correct → re-test. |
| `worker/calibration/prompts.py` | New. Prompt templates for Opus 4.6 behavioral scoring, contextual scoring, and correction rule generation. |
| `worker/pipeline/drafts.py` | Add `CALIBRATION RULES:` section to draft prompt between personality profile and NEVER list. |
| `worker/supabase_client.py` | Add calibration state fields: `calibration_status`, `calibration_rules`, `calibration_iteration`, `calibration_scores`. |
| `worker/run_pipeline.py` | Add calibration gate check before draft generation. Skip drafting if `calibration_status != 'passed'`. |
| Supabase migration | Add calibration columns to `profiles` table. Add `calibration_results` table. |

## Step-by-Step Implementation

### Step 1: Test Email Selection (`email_selector.py`)

**Goal:** Select 15 emails from the user's history that provide maximum coverage across contact types, complexity levels, and signal profiles. Include emails the user did not reply to for NO_DRAFT_NEEDED calibration.

**Selection algorithm:**

```
select_calibration_emails(user_id) → list[CalibrationEmail]
```

**Stratification targets:**

| Axis | Bucket | Target Count |
|------|--------|-------------|
| Contact type | Internal colleague | 5–6 |
| Contact type | External professional (lender, legal, investor) | 4–5 |
| Contact type | External casual (vendor, contractor) | 3–4 |
| Contact type | Personal | 1–2 |
| Complexity | Standalone single-message reply | 4–5 |
| Complexity | Mid-complexity thread (2–4 messages) | 4–5 |
| Complexity | Complex thread (5+ messages, multiple participants) | 3–4 |
| Reply status | User did NOT reply (NO_DRAFT_NEEDED ground truth) | 2–3 |
| Signal profile | `has_material_consequence` fired | ≥ 2 |
| Signal profile | `has_action_request` fired | ≥ 2 |
| Signal profile | `user_is_bottleneck` fired | ≥ 2 |

**Selection constraints:**
- Only select emails where the full thread is available in the system
- Only select emails where the user's actual sent reply is captured (for reply emails)
- For NO_DRAFT_NEEDED candidates, select emails the user received but verifiably did not reply to within 72 hours
- Prefer emails from the most recent 30 days of history for relevance
- If a bucket is underrepresented in user history, reduce its target and redistribute — don't force representation of registers that barely exist

**Data structure:**

```python
@dataclass
class CalibrationEmail:
    db_id: str
    incoming_email: dict          # The email the user received
    thread_emails: list[dict]     # Full thread history
    user_reply: str | None        # The user's actual sent reply (None = no reply)
    contact_type: str             # From contact record
    thread_depth: int             # Number of messages in thread
    signal_scores: dict           # Extracted signals from original pipeline run
    selection_bucket: str         # Which stratification bucket this fills
```

**Implementation notes:**
- Query sent mail table joined with inbox table on thread/conversation ID
- Query signal scores from existing signal extraction results
- Query contact types from contacts table
- Use greedy allocation: iterate through emails sorted by recency, assign each to the most underrepresented bucket it qualifies for, stop at 15

### Step 2: Ground Truth Scoring — Layer 1 (`ground_truth_scorer.py`)

**Goal:** Score the user's actual sent email against every profile dimension to establish what the user *actually did* on each axis. This is the ground truth the generated draft will be compared against.

**Scoring dimensions:**

#### Style Dimensions (mechanical — no LLM needed)

```python
@dataclass
class StyleScore:
    word_count: int
    sentence_count: int
    greeting_type: str            # none | minimal | professional | warm
    signoff_type: str             # none | signature_block | simple_closing | warm_closing
    uses_contractions: bool
    uses_bullets: bool
    exclamation_count: int
    question_count: int
```

**Greeting classification rules:**
- `none`: No greeting, jumps straight to content
- `minimal`: "Hey [Name]," or "Hey guys,"
- `professional`: "Hi [Name]," or "Hi [Name] –"
- `warm`: Greeting with enthusiastic language, "Thanks [Name]!" as opener

**Sign-off classification rules:**
- `none`: No sign-off, message ends with content
- `signature_block`: Full title/contact signature
- `simple_closing`: "Best," "Thanks!" "Best regards,"
- `warm_closing`: Enthusiastic/personal closing

**Implementation:** Regex and string matching. No API calls needed.

#### Behavioral Dimensions (Opus 4.6-scored)

```python
@dataclass
class BehavioralScore:
    decisiveness: str             # decides | proposes_solution | defers | delegates | no_signal
    thoroughness: str             # addresses_all | key_point_only | no_signal
    specificity: str              # specific_next_step | conditional_decision | vague_forward | no_signal
```

**Prompt template** (`prompts.py`):

```
You are scoring an email reply against behavioral dimensions.
The user's actual sent reply is provided below.

Classify the reply on each dimension. Output exactly three lines:
DECISIVENESS: {decides | proposes_solution | defers | delegates | no_signal}
THOROUGHNESS: {addresses_all | key_point_only | no_signal}
SPECIFICITY: {specific_next_step | conditional_decision | vague_forward | no_signal}

No explanation. No other text.

ORIGINAL EMAIL (what the user was replying to):
{incoming_email_body}

USER'S REPLY:
{sent_email_body}
```

**Params:** Model: opus-4.6, max_tokens: 50, temperature: 0.

#### Preference Dimensions (Opus 4.6-scored)

```python
@dataclass
class PreferenceScore:
    investment_signal: str        # active | selective | conservative | no_signal
    positional_signal: str        # advancing | measured | yielding | no_signal
```

**Prompt template** (`prompts.py`):

```
You are scoring an email reply against preference dimensions.

Classify the reply on each dimension. Output exactly two lines:
INVESTMENT: {active | selective | conservative | no_signal}
POSITIONAL: {advancing | measured | yielding | no_signal}

No explanation. No other text.

ORIGINAL EMAIL (what the user was replying to):
{incoming_email_body}

THREAD CONTEXT (if any):
{thread_summary_or_none}

USER'S REPLY:
{sent_email_body}
```

**Params:** Model: opus-4.6, max_tokens: 30, temperature: 0.

#### Contextual Dimensions (metadata — no LLM needed for Layer 1)

```python
@dataclass
class ContextualGroundTruth:
    user_replied: bool            # Did the user send a reply at all?
    reply_word_count: int | None  # Length of actual reply (None if no reply)
    user_is_primary_recipient: bool  # Is user in To: vs Cc:?
    thread_depth: int
```

**Full Layer 1 output:**

```python
@dataclass
class GroundTruthScore:
    email_id: str
    style: StyleScore
    behavioral: BehavioralScore
    preference: PreferenceScore
    context: ContextualGroundTruth
```

### Step 3: Draft Generation for Test Emails

**Goal:** Run each test email through the production draft pipeline as if it were a new incoming email, producing a draft (or NO_DRAFT_NEEDED) for comparison.

**Process:**
1. For each `CalibrationEmail` where `user_reply is not None`, run through the full draft pipeline:
   - Generate Haiku thread summary (using the new structured format from the prompt rework)
   - Look up contacts for all participants
   - Build the draft prompt with personality profile + NEVER list (no calibration rules on first iteration)
   - Generate Sonnet draft
2. For each `CalibrationEmail` where `user_reply is None`, run through the full draft pipeline identically — the model itself outputs `NO_DRAFT_NEEDED: <reason>` when it determines no reply is warranted. There is no separate pre-filter; the should-draft decision is made by the same Opus 4.6 draft call
3. Store: the generated draft text, the thread summary used, and whether the system decided to draft or not

**Important:** Use the exact same pipeline code and prompt structure that live drafting will use. The calibration test must be a faithful simulation, not a separate code path. Call the existing `DraftGenerator.generate_draft()` and `DraftGenerator.build_batch_params()` directly.

**Thread summary caching:** Generate thread summaries once during iteration 1 and cache them. Reuse the cached summaries for iterations 2 and 3 so that the only variable changing between iterations is the calibration rules — not stochastic variation in summaries.

On subsequent iterations (2 and 3), include the calibration rules generated from the prior iteration in the prompt.

### Step 4: Draft Comparison Scoring — Layer 2 (`draft_scorer.py`)

**Goal:** Compare the generated draft against the Layer 1 ground truth across all dimensions. Produce a match/adjacent/hard_miss classification for each dimension.

#### Style Delta (mechanical comparison)

```python
@dataclass
class StyleDelta:
    word_count_ratio: float        # draft_words / actual_words (target: 0.7–1.5)
    greeting_match: str            # match | adjacent | hard_miss
    signoff_match: str             # match | adjacent | hard_miss
    contraction_match: str         # match | mismatch
    bullet_match: str              # match | mismatch
    formality_register: str        # match | adjacent | hard_miss
```

**Greeting match rules:**
- `match`: Exact same type (e.g., both `none`, both `minimal`)
- `adjacent`: One step apart (e.g., `none` vs `minimal`, `minimal` vs `professional`)
- `hard_miss`: Two or more steps apart (e.g., `none` vs `professional`, `minimal` vs `warm`)

**Sign-off match rules:** Same adjacency logic.

**Formality register:** Derived from the combination of greeting + sign-off + contraction usage + sentence structure. Internal colleague with "Hi [Name]," + "Best regards," = hard miss on register.

**Word count ratio thresholds:**
- `match`: 0.7–1.5x
- `adjacent`: 0.4–0.7x or 1.5–2.5x
- `hard_miss`: Under 0.4x or over 2.5x

#### Behavioral Delta (comparison of Haiku classifications)

Run the same Opus 4.6 behavioral scoring prompt from Layer 1 on the generated draft. Then compare:

```python
@dataclass
class BehavioralDelta:
    decisiveness_match: str        # match | adjacent | hard_miss
    thoroughness_match: str        # match | adjacent | hard_miss
    specificity_match: str         # match | adjacent | hard_miss
```

**Adjacency rules for decisiveness:**
- `match`: Same signal
- `adjacent`: One step apart on the scale (decides ↔ proposes_solution, proposes_solution ↔ defers, defers ↔ delegates)
- `hard_miss`: Two+ steps apart (decides vs defers, proposes_solution vs delegates)

**Adjacency rules for thoroughness:**
- `match`: Same signal
- `adjacent`: addresses_all ↔ key_point_only only if the email was low-stakes
- `hard_miss`: addresses_all vs key_point_only on a high-stakes or multi-point email

**Adjacency rules for specificity:**
- `match`: Same signal
- `adjacent`: One step apart (specific_next_step ↔ conditional_decision, conditional_decision ↔ vague_forward)
- `hard_miss`: specific_next_step vs vague_forward, or vague_forward vs specific_next_step

#### Preference Delta (comparison of Haiku classifications)

Same approach — run Opus 4.6 preference scoring on the generated draft, compare to Layer 1:

```python
@dataclass
class PreferenceDelta:
    investment_match: str          # match | adjacent | hard_miss | not_applicable
    positional_match: str          # match | adjacent | hard_miss | not_applicable
```

If both the actual email and the draft scored `no_signal`, mark as `not_applicable` — don't penalize or reward.

#### Contextual Scoring (Opus 4.6-as-judge for content dimensions)

```python
@dataclass
class ContextualScore:
    should_draft_accuracy: str     # correct | incorrect
    content_alignment: str         # match | partial | hard_miss
    fabrication_detected: bool     # Any commitments, facts, or references not in source?
    comprehension_pass: bool       # Did draft correctly understand what was being asked?
    attribution_pass: bool         # Did draft respond to the right person about the right thing?
```

**Should-draft accuracy (mechanical):**
- `correct`: System drafted AND user replied, OR system flagged NO_DRAFT_NEEDED AND user did not reply
- `incorrect`: System drafted AND user did not reply, OR system flagged NO_DRAFT_NEEDED AND user did reply

**Content, fabrication, comprehension, attribution (Opus 4.6-as-judge):**

**Prompt template** (`prompts.py`):

```
You are evaluating a generated email draft against the email the user
actually sent. Score the draft on four dimensions.

ORIGINAL INCOMING EMAIL:
{incoming_email}

THREAD CONTEXT:
{thread_summary}

USER'S ACTUAL SENT REPLY:
{actual_reply}

GENERATED DRAFT:
{generated_draft}

Score each dimension. Output exactly four lines:
CONTENT_ALIGNMENT: {match | partial | hard_miss} — Did the draft make the same substantive decision as the user?
FABRICATION: {none | detected} — Did the draft commit to, reference, or state anything not present in the incoming email or thread? This includes fabricated action items, deadlines, attachment references, and self-generated commitments.
COMPREHENSION: {pass | fail} — Did the draft correctly understand what was being asked or discussed?
ATTRIBUTION: {pass | fail} — Did the draft respond to the right person about the right thing, and correctly identify whether the user was the appropriate respondent?

No explanation. No other text.
```

**Params:** Model: opus-4.6, max_tokens: 80, temperature: 0.

#### Full Layer 2 Output

```python
@dataclass
class CalibrationResult:
    email_id: str
    iteration: int
    ground_truth: GroundTruthScore
    generated_draft: str | None     # None if NO_DRAFT_NEEDED
    style_delta: StyleDelta
    behavioral_delta: BehavioralDelta
    preference_delta: PreferenceDelta
    contextual: ContextualScore
    overall: str                    # pass | soft_miss | hard_miss
```

**Overall classification:**
- `pass`: All dimensions are match or not_applicable
- `soft_miss`: At least one adjacent miss, no hard misses, no contextual failures
- `hard_miss`: At least one hard miss on any dimension, OR any contextual failure (fabrication detected, comprehension fail, attribution fail, should-draft incorrect)

### Step 5: Correction Rule Generation (`correction_generator.py`)

**Goal:** Cluster hard misses from the test batch, generate specific correction rules, and prepare them for injection into the draft prompt.

**Process:**

1. Collect all `CalibrationResult` entries with `overall == "hard_miss"`
2. Group by failure dimension (e.g., all greeting hard misses, all fabrication failures, all should-draft errors)
3. For each group, generate a correction rule

**Correction rule generation approach:**

For style and behavioral hard misses, generate rules mechanically from the pattern:

```python
def generate_style_correction(failures: list[CalibrationResult]) -> str:
    # Example: 4 emails to internal colleagues all had greeting hard miss
    # Pattern: draft used "Hi [Name]," but user used no greeting
    contact_types = [f.ground_truth.context.contact_type for f in failures]
    if len(set(contact_types)) == 1:
        return f"When replying to {contact_types[0]} contacts, use no greeting. Jump straight into content."
    else:
        return "Default to no greeting unless replying to external lenders or legal contacts."
```

For contextual hard misses (fabrication, comprehension, attribution), use Opus 4.6 to generate the correction rule from the specific failure:

**Prompt template** (`prompts.py`):

```
You are analyzing draft failures to generate a specific correction rule.

Below are email drafts that failed quality checks. For each, you are shown
the failure type and what went wrong.

{for each failure:}
FAILURE {n}:
Type: {fabrication | comprehension | attribution | should_draft}
Incoming email summary: {one-line summary}
What the draft did wrong: {description}
What the user actually did: {actual reply or "did not reply"}

Generate the minimum number of correction rules that would prevent ALL
of these failures. Each rule should be:
- Specific and behavioral (not vague like "be more careful")
- Testable (you could verify compliance mechanically or with a simple check)
- Scoped (applies to the specific situation, not a blanket override)

Output one rule per line, prefixed with "- ". No other text.
```

**Params:** Model: opus-4.6, max_tokens: 300, temperature: 0.

**Correction rule storage format:**

```python
@dataclass
class CalibrationRule:
    rule_text: str                 # The actual rule to inject into the prompt
    source_dimension: str          # Which scoring dimension triggered it
    source_failures: list[str]     # Email IDs that caused this rule
    iteration_added: int           # Which calibration iteration generated it
```

**Constraints:**
- Maximum 10 correction rules total across all iterations
- When the cap is reached, rank rules by severity (hard_miss weight) × frequency (number of source failures). Drop the lowest-ranked rule to make room for a new one rather than truncating by position
- Rules from later iterations do not contradict rules from earlier iterations
- Rules do not contradict the personality profile — if they would, flag for profile revision instead

### Step 6: Prompt Integration (`drafts.py`)

**Goal:** Inject calibration rules into the draft prompt structure between the personality profile and the NEVER list.

**Updated prompt structure:**

```
PERSONALITY PROFILE:
{style_guide}

{behavioral_profile}

{preference_profile}

CALIBRATION RULES:
{calibration_rules, one per line, or "None." if no rules generated}

NEVER:
{guardrail list}

EMAIL:
From: {sender_name} <{sender_email}>
Sent: {received_time}
To: {to_field}
Cc: {cc_field}
Subject: {subject}

{isolated, truncated body}

THREAD SUMMARY:
{thread_summary}

CONTACT SUMMARY:
{one-line per contact}

Draft a reply. If no response is needed, output only: NO_DRAFT_NEEDED: <reason>
```

**Implementation in `_build_calibration_section()`:**

```python
def _build_calibration_section(self, action_context: dict) -> str:
    rules = action_context.get("calibration_rules", [])
    if not rules:
        return "CALIBRATION RULES:\nNone."
    rules_text = "\n".join(f"- {r}" for r in rules)
    return f"CALIBRATION RULES:\n{rules_text}"
```

**Loading calibration rules:** Read from `profiles.calibration_rules` (text column) at pipeline startup, alongside style_guide, behavioral_profile, and preference_profile.

### Step 7: Calibration Orchestrator (`runner.py`)

**Goal:** Run the full calibration loop: select → score → draft → compare → correct → re-test.

```python
async def run_calibration(user_id: str, api_key: str) -> CalibrationOutcome:
    """
    Main calibration loop. Returns outcome with status and rules.
    """
    # Select test emails
    test_emails = select_calibration_emails(user_id)

    # Score ground truth (Layer 1) — only runs once
    # All 15 emails scored concurrently via asyncio.gather
    ground_truth = await score_ground_truth_batch(test_emails)

    calibration_rules = []
    cached_thread_summaries = None
    max_iterations = 3

    for iteration in range(1, max_iterations + 1):
        # Generate test drafts using current profile + accumulated rules
        # Cache thread summaries from iteration 1; reuse on subsequent iterations
        drafts, thread_summaries = generate_test_drafts(
            test_emails, calibration_rules, cached_thread_summaries
        )
        if cached_thread_summaries is None:
            cached_thread_summaries = thread_summaries

        # Score drafts against ground truth (Layer 2) — batched concurrently
        results = await score_drafts_batch(drafts, ground_truth)

        # Check exit criteria
        if meets_exit_criteria(results):
            store_calibration_results(user_id, "passed", calibration_rules, results)
            return CalibrationOutcome(status="passed", rules=calibration_rules, iterations=iteration)

        # Generate corrections from hard misses
        hard_misses = [r for r in results if r.overall == "hard_miss"]
        new_rules = generate_corrections(hard_misses)
        calibration_rules.extend(new_rules)

        # Cap total rules at 10, ranked by severity × frequency (drop lowest-ranked)
        if len(calibration_rules) > 10:
            calibration_rules = rank_and_trim_rules(calibration_rules, max_rules=10)

        # Store intermediate results
        store_calibration_results(user_id, f"iteration_{iteration}", calibration_rules, results)

    # Failed to converge after 3 iterations
    store_calibration_results(user_id, "needs_review", calibration_rules, results)
    return CalibrationOutcome(status="needs_review", rules=calibration_rules, iterations=max_iterations)
```

**Batching strategy:**
- All 15 ground truth scoring calls (Layer 1) run concurrently via `asyncio.gather` — behavioral + preference scoring for each email fire in parallel
- All 15 draft generation calls run concurrently (or in batches of 5 if rate-limited)
- All 15 Layer 2 scoring calls (behavioral + preference + contextual judge) run concurrently
- Correction rule generation is a single call after all scoring completes
- Use a semaphore (e.g., `asyncio.Semaphore(10)`) to cap concurrent API requests and avoid rate limits

### Step 8: Exit Criteria

**The calibration loop exits when ALL of the following are met:**

| Dimension | Metric | Threshold | Measurement |
|-----------|--------|-----------|-------------|
| Style | Greeting match rate | ≥ 90% match or adjacent | Count of non-hard-miss / total |
| Style | Sign-off match rate | ≥ 90% match or adjacent | Count of non-hard-miss / total |
| Style | Word count ratio | ≥ 90% within 0.4–2.5x | Count within range / total |
| Behavioral | Decisiveness match | ≥ 80% match or adjacent | Excluding no_signal pairs |
| Behavioral | Thoroughness match | ≥ 80% match or adjacent | Excluding no_signal pairs |
| Behavioral | Specificity match | ≥ 80% match or adjacent | Excluding no_signal pairs |
| Preference | Investment match | ≥ 75% match or adjacent | Excluding not_applicable |
| Preference | Positional match | ≥ 75% match or adjacent | Excluding not_applicable |
| Contextual | Should-draft accuracy | ≥ 85% | ≤ 2 incorrect classifications out of 15 |
| Contextual | Fabrication rate | ≤ 15% | ≤ 2 fabrications detected out of 15 |
| Contextual | Comprehension rate | ≥ 85% | ≤ 2 comprehension failures out of 15 |
| Contextual | Attribution rate | ≥ 85% | ≤ 2 attribution failures out of 15 |

```python
def meets_exit_criteria(results: list[CalibrationResult]) -> bool:
    style_pass = (
        match_rate(results, "greeting") >= 0.90
        and match_rate(results, "signoff") >= 0.90
        and word_count_ratio_pass_rate(results) >= 0.90
    )
    behavioral_pass = (
        match_rate(results, "decisiveness") >= 0.80
        and match_rate(results, "thoroughness") >= 0.80
        and match_rate(results, "specificity") >= 0.80
    )
    preference_pass = (
        match_rate(results, "investment") >= 0.75
        and match_rate(results, "positional") >= 0.75
    )
    contextual_pass = (
        should_draft_accuracy(results) >= 0.85
        and fabrication_rate(results) <= 0.15
        and comprehension_rate(results) >= 0.85
        and attribution_rate(results) >= 0.85
    )
    return style_pass and behavioral_pass and preference_pass and contextual_pass
```

**Failure after 3 iterations:**
- Store all results and rules
- Set `calibration_status = 'needs_review'`
- Flag the specific dimensions that did not converge
- Do not enable live drafting
- Surface to user: "Your drafting profile needs adjustment before we can start generating drafts."

**Retry mechanism for `needs_review`:**
1. Present the user with a summary of which dimensions failed and why
2. Allow the user to trigger a re-calibration via the dashboard or an API call
3. On retry, re-run onboarding synthesis for the failing dimensions only (e.g., regenerate the behavioral profile if behavioral dimensions failed), then re-enter the calibration loop from iteration 1 with a fresh set of 15 test emails
4. Track retry count in `profiles.calibration_retry_count`. Cap at 3 retries before escalating to manual support
5. Retry resets `calibration_status` back to `'running'` and clears prior `calibration_results` for that user

### Step 9: Supabase Migration

```sql
-- Add calibration columns to profiles table
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS calibration_status text DEFAULT 'pending'
    CHECK (calibration_status IN ('pending', 'running', 'passed', 'needs_review')),
  ADD COLUMN IF NOT EXISTS calibration_rules text DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS calibration_iteration integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS calibration_retry_count integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS calibrated_at timestamptz DEFAULT NULL;

-- Create calibration_results table for auditability
CREATE TABLE IF NOT EXISTS public.calibration_results (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  iteration integer NOT NULL,
  email_id text NOT NULL,
  ground_truth jsonb NOT NULL,
  generated_draft text,
  style_delta jsonb NOT NULL,
  behavioral_delta jsonb NOT NULL,
  preference_delta jsonb NOT NULL,
  contextual_scores jsonb NOT NULL,
  overall_result text NOT NULL CHECK (overall_result IN ('pass', 'soft_miss', 'hard_miss')),
  correction_rules_applied text[],
  created_at timestamptz DEFAULT now()
);

-- Index for querying by user and iteration
CREATE INDEX idx_calibration_results_user_iteration
  ON public.calibration_results(user_id, iteration);

-- RLS policies
ALTER TABLE public.calibration_results ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own calibration results"
  ON public.calibration_results FOR SELECT
  USING (auth.uid() = user_id);
```

### Step 10: Pipeline Gate (`run_pipeline.py`)

**Goal:** Prevent draft generation for users who have not passed calibration.

Insert before Stage 5 (draft generation):

```python
# Check calibration gate
profile = db.get_profile(user_id)
if profile.get("calibration_status") != "passed":
    logger.info(f"Skipping draft generation: calibration_status = {profile.get('calibration_status')}")
    # Still run signal extraction, triage, summaries — just skip drafting
    return
```

**Calibration trigger:** After onboarding completes successfully, automatically queue a calibration run:

```python
# In onboarding completion handler
if onboarding_complete:
    db.update_profile(user_id, {"calibration_status": "running"})
    await run_calibration(user_id, api_key)
```

## Cost and Performance Estimates

**Per iteration (15 test emails):**

All calls use Opus 4.6 ($15/M input, $75/M output).

| Call | Count | Est. tokens/call | Est. cost |
|------|-------|-------------------|-----------|
| Thread summaries | ~10 (threaded emails only, cached after iter 1) | 800 in / 400 out | $0.42 |
| Draft generation | ~12 (excluding NO_DRAFT_NEEDED) | 1500 in / 300 out | $0.54 |
| Behavioral scoring (ground truth, iter 1 only) | 12 | 500 in / 50 out | $0.13 |
| Behavioral scoring (draft) | 12 | 500 in / 50 out | $0.13 |
| Preference scoring (ground truth, iter 1 only) | 12 | 600 in / 30 out | $0.13 |
| Preference scoring (draft) | 12 | 600 in / 30 out | $0.13 |
| Contextual scoring (Opus-as-judge) | 12 | 2000 in / 80 out | $0.43 |
| Correction generation | 1 | 1500 in / 300 out | $0.04 |
| **Total iteration 1** | | | **~$1.95** |
| **Total iteration 2–3** (no thread summaries or ground truth scoring) | | | **~$1.27** |

**Full calibration (up to 3 iterations): ~$4.49**

**Note:** Cost optimization is a future concern — Opus 4.6 is used across the board to maximize quality during initial implementation. Scoring calls are strong candidates for downgrading to Haiku/Sonnet once calibration accuracy is validated.

**Time estimate:** ~5–8 minutes per iteration with concurrent batching. Full calibration completes within 15–25 minutes worst case.

## Out of Scope

- **Passive calibration from edit diffs** — future work. Track user edits to generated drafts and feed diffs back into correction rules on an ongoing basis. Builds on this architecture but requires edit tracking infrastructure.
- **Automatic profile revision** — if calibration reveals the personality profile itself is wrong (not just incomplete), the current design flags for review rather than auto-correcting the profile. Future work could close this loop.
- **User-facing calibration UI** — no UI for now. All results stored in Supabase `calibration_results` table for direct inspection.
- **Re-calibration triggers** — when to re-run calibration (e.g., after profile updates, after N weeks, after user feedback). Deferred to future iteration.
- **Cross-user calibration insights** — using calibration failures across users to improve the base prompts or onboarding synthesis. Deferred.

## Verification

1. Run calibration on a test user with known email history. Verify 15 emails are selected with correct stratification.
2. Verify Layer 1 scoring produces reasonable classifications for a set of manually reviewed sent emails.
3. Verify Layer 2 scoring correctly identifies known failure modes (over-explanation, fabricated commitments, wrong register, NO_DRAFT_NEEDED misses).
4. Verify correction rules generated from hard misses are specific, testable, and non-contradictory.
5. Verify second iteration drafts improve on first iteration for the specific failures that were corrected.
6. Verify exit criteria gate works — drafts are not generated for live emails until calibration passes.
7. Verify calibration results are stored in Supabase with full auditability.
8. Verify pipeline gate correctly checks `calibration_status` before entering draft generation stage.
9. Verify cost per calibration run is within estimated range.
10. Verify calibration rules are correctly injected into draft prompt structure.
11. Verify all calibration data (ground truth, generated drafts, scoring deltas, correction rules) is queryable in Supabase `calibration_results` table.
