# Preference Layer — Design & Implementation Plan

## The Problem

Clarion's draft generation pipeline has three layers: Style Guide (how you write), Behavioral Profile (how you express decisions), and a gap — nothing tells the model **what the user would actually decide**. Without this signal, the model either guesses from domain heuristics or punts behind `[USER TO CONFIRM]` menus. The preference layer fills the gap.

---

## Why Qualitative Categories, Not Numeric Scores

The original design used a 1–10 numeric scale per trait. Three problems emerged:

**1. The dead zone.** A score of 5–6 tells the model "this person sometimes invests and sometimes doesn't" — which is functionally identical to having no preference signal at all. The draft hedges, which is exactly the behavior the preference layer was supposed to eliminate. Numeric scales invite regression to the mean because the middle feels safe and defensible.

**2. The barbell problem.** Most users' observable decisions cluster toward the "active" end of the spectrum because sent emails are biased toward action — decisions where the user chose NOT to act often don't generate a sent email. Trying to correct for this bias mathematically (applying deflation factors, constraining low-confidence scores to the center) pushes scores toward the middle, which compounds the dead zone problem. The more you try to be precise, the less useful the scores become.

**3. False precision.** The difference between a 6 and a 7 on Investment Orientation is undefined in practice. What does the model do differently with a 7 vs. a 6? If the answer is "not much," then the numeric granularity adds complexity without adding signal. Meanwhile, the difference between "this user defaults to investing" and "this user defaults to conserving" is clear and actionable — the model drafts in opposite directions.

Qualitative categories solve all three problems. There is no middle to hide in — Sonnet must pick a side. The observation bias matters less because you're asking "which side does this user default to?" rather than "what exact ratio of active-to-conservative decisions did they produce?" And the categories are self-documenting — "Invest Heavy" tells the draft model exactly what to do without interpreting a number on a scale.

---

## The Two Traits

### Investment Orientation

**Core question:** When something is suboptimal, broken, risky, or could be improved — does this user invest to address it?

Four categories, no center option:

**INVEST HEAVY** — The user's default is to invest. This shows up consistently across decisions, including low-stakes situations where most people would accept good-enough. They shop vendors, close coverage gaps, fix problems fully, investigate root causes, explore alternatives, and act preemptively. When they see a gap between current state and better state, they act to close it.

**INVEST LIGHT** — The user leans toward investing but exercises judgment. They invest on decisions with clear impact — shopping a significant renewal, adding coverage for a known gap — but may accept the default on lower-stakes items or when the cost-benefit is marginal. Their default is action, but they don't invest reflexively.

**CONSERVE LIGHT** — The user leans toward conservation but isn't passive. They accept the status quo on most decisions — renew without shopping, skip optional protections, take the partial fix — but will invest when a problem is urgent or the cost of inaction is obvious. Their default is inaction, but they're not negligent.

**CONSERVE HEAVY** — The user's default is to conserve. They consistently accept the status quo, minimize expenditure, wait for problems to force action, and choose partial remedies. This shows up even in situations where investing would clearly be the better long-term play.

### Positional Stance

**Core question:** When this user's interests intersect with another party's — or when an expert recommends a course of action — does the user yield or advance?

Four categories, no center option:

**ADVANCE HEAVY** — The user's default is to push. They negotiate concessions, demand reciprocity when yielding, exploit situational leverage, escalate commitment when competing, and pressure-test expert recommendations. They challenge the easy path when a harder path offers more control over outcomes. They do this consistently, including in low-stakes situations where most people would accommodate.

**ADVANCE LIGHT** — The user leans toward advancing but picks their spots. They push back on significant asks, negotiate on high-stakes items, and independently evaluate expert recommendations in their domain of expertise. On lower-stakes interactions or topics outside their expertise, they may accommodate or follow guidance. Their default is to hold ground, but they're not combative.

**YIELD LIGHT** — The user leans toward accommodation but isn't a pushover. They generally follow expert guidance, concede without demanding reciprocity, and prefer collaborative resolution. On high-stakes matters where yielding has clear and obvious cost, they may hold ground or negotiate. Their default is to accommodate, but they'll push when it's clearly necessary.

**YIELD HEAVY** — The user's default is to accommodate. They accept requests, follow expert guidance without independent evaluation, concede without conditions, walk away rather than escalate, and let advantageous positions pass unexploited. This shows up even in situations where pushing back would be clearly beneficial.

---

## How Categories Compose with the Existing Layers

The preference layer determines the DIRECTION of a decision. The behavioral profile determines the EXPRESSION of that decision. The style guide determines the VOICE in which it's written.

| Layer | Question | Example |
|-------|----------|---------|
| **Preference** | What to decide | Invest in the full remedy, not the partial fix |
| **Behavior** | How to express it | Decide firmly, propose conditionally, or defer |
| **Style** | How to write it | Terse and formal, or warm and thorough |

### Composition Examples

| Scenario | Investment | Positional | Behavioral | Draft Output |
|----------|-----------|-----------|-----------|-------------|
| Bobby recommends BI coverage at $3,200/yr | Invest Heavy | Yield Light | B1 (decisive) | "Add the BI coverage. You're right, the $3,200 is reasonable protection given our exposure." |
| Bobby recommends BI coverage at $3,200/yr | Invest Heavy | Advance Light | B1 (decisive) | "I want to add BI coverage, but send me the benefit limits and exclusions before I commit to the $3,200." |
| Bobby recommends BI coverage at $3,200/yr | Conserve Light | Yield Light | B2 (deferential) | "What's your professional take on whether we really need the BI coverage given our property type?" |
| Opposing counsel requests 30-day extension | Invest Heavy | Advance Heavy | B3 (collaborative) | "I'd recommend a 15-day compromise, conditioned on their privilege log by April 14th and our preferred deposition dates locked in." |
| Competing offer on property, $10K gap | Conserve Light | Yield Light | B1 (decisive) | "Walk away. The $10K gap plus financing disadvantage makes this a losing proposition. Schedule showings on the alternatives." |

### Tiebreaker Rule

When the two traits point in different directions on a given decision:

1. **Higher preference wins** — the trait with the stronger (heavier) category takes priority (e.g., Invest Heavy overrides Yield Light)
2. **If categories are equally weighted** — the trait with higher confidence takes priority
3. **If confidence is also equal** — take the more cautious direction and mark as `[USER TO CONFIRM]`

---

## Extraction Pipeline

### Pipeline Position

```
Phase 3       — email features          (Haiku, parallel)     ← unchanged
Phase 4C-1    — style features          (Haiku, parallel)     ← unchanged
Phase 4C-1b   — behavioral features     (Haiku, parallel)     ← MODIFIED (adds decision detection fields)

Phase 4A      — contact synthesis       (Sonnet, sequential)  ← unchanged

Phase 4B      — topic synthesis         (Sonnet, parallel)    ← unchanged
Phase 4C-2    — style guide synthesis   (Sonnet, parallel)    ← unchanged
Phase 4C-3    — behavioral synthesis    (Sonnet, parallel)    ← unchanged
Phase 4C-4    — preference synthesis    (Sonnet, parallel)    ← NEW

Phase 7       — model training                                ← unchanged
```

Phase 4C-4 has the same dependency pattern as 4C-3: needs Haiku extraction complete + contact profiles for context. It runs in parallel with 4B, 4C-2, and 4C-3 since none depend on each other.

### Phase 1: Haiku Decision Detection

Haiku identifies decision moments in sent emails and extracts raw evidence. It does NOT classify decisions along trait dimensions — that's interpretive reasoning that Haiku produces noisy signals on.

Decision detection fields are added to the existing `HAIKU_BEHAVIORAL_EXTRACTION_PROMPT`. The behavioral extraction already processes sent+received pairs and examines decision disposition — decision detection is the same cognitive tier (pattern recognition). No new parallel phase, no new failure mode, no new future to track.

Added extraction fields per email (alongside existing style and behavioral features):

```json
{
  "contains_decision": true,
  "decision_quote": "Let's go ahead and renew — I compared three vendors and this is still the best fit for what we need"
}
```

Haiku identifies the decision moment and extracts a verbatim quote. It does **not** classify direction (active/conservative, advancing/yielding) or summarize — Sonnet performs all interpretation from the raw quotes.

Not every email contains a decision. Emails that are purely informational, scheduling, or forwarding will return `"contains_decision": false`. The proportion of decision-bearing emails will vary significantly by user — a CFO may have 60–70% decision-bearing emails, while an operations coordinator may have 15–20%. The pipeline does not assume a fixed ratio.

**No `decision_summary` field.** The original design included a summary, but since Sonnet performs all interpretation from raw quotes, having Haiku summarize adds an unnecessary abstraction layer that could distort the signal.

**Token impact:** ~20-40 additional output tokens per email pair. At batches of 10, this adds ~200-400 tokens per batch. Manageable within existing Haiku rate limits.

### Phase 2: Sonnet Classification + Synthesis

Sonnet receives the full batch of decision quotes and performs classification and synthesis in a single call.

**Classification — per decision:**

For each decision moment, Sonnet classifies the direction along each trait:

- **Investment Orientation:** `active`, `selective`, `conservative`, or `no_signal`
- **Positional Stance:** `advancing`, `measured`, `yielding`, or `no_signal`

`selective` and `measured` are the Light vs Heavy discriminators. A user whose active signals are predominantly `selective` (invested, but only after weighing cost-benefit) trends toward Invest Light. A user whose active signals are predominantly unqualified `active` trends toward Invest Heavy. Same logic applies for `measured` vs raw `advancing`.

`no_signal` means the decision provides NO evidence for that trait. It is NOT a middle ground. Decisions marked `no_signal` are excluded from the count for that trait.

**Synthesis — per trait:**

After classifying all decisions, Sonnet assigns a category based on the distribution of signals:

```
CLASSIFICATION RULES:

Pick the category that describes this user's DEFAULT across the
full decision set. Look for the pattern, not the average.

There is no middle option. Pick a side.

INVEST HEAVY: Dominant pattern is "active" signals across
multiple decision types, including low-stakes decisions where
most people would accept good-enough. Few or no "conservative"
signals.

INVEST LIGHT: Majority of signals are "active" or "selective."
The user invests on important decisions but exercises judgment
on lower-stakes items. Distinguished from Invest Heavy by the
presence of "selective" or "conservative" signals on lower-stakes
decisions.

CONSERVE LIGHT: Majority of signals are "conservative." The user
defaults to the status quo but invests when urgency or obvious
cost of inaction demands it. Distinguished from Conserve Heavy by
the presence of "active" signals on high-stakes decisions.

CONSERVE HEAVY: Dominant pattern is "conservative" signals across
multiple decision types, including situations where investing
would clearly be the better play. Few or no "active" signals.

ADVANCE HEAVY: Dominant pattern is "advancing" signals across
multiple interaction types, including low-stakes situations.
Few or no "yielding" signals.

ADVANCE LIGHT: Majority of signals are "advancing" or "measured."
The user pushes on significant matters but accommodates on lesser
points. Distinguished from Advance Heavy by the presence of
"measured" or "yielding" signals on lower-stakes interactions.

YIELD LIGHT: Majority of signals are "yielding." The user
defaults to accommodation but holds ground when the cost of
yielding is obvious. Distinguished from Yield Heavy by the
presence of "advancing" signals on high-stakes matters.

YIELD HEAVY: Dominant pattern is "yielding" signals across
multiple interaction types, including situations where pushing
back would clearly be beneficial. Few or no "advancing" signals.

OBSERVATION BIAS NOTE:
Sent emails are biased toward action. Ask: "what is this user's
default when they have a genuine choice?" not "what is the ratio
of active to conservative in the data?"
```

Sonnet then writes a 2–3 sentence personality sketch specific to the user's decisions. The sketch captures not just the category but how the user reasons about decisions — whether they lead with cost-benefit analysis, relationship impact, risk mitigation, or some other lens. This reasoning style emerges naturally from the decision quotes and doesn't require a separate classification; it's embedded in the description so the draft model can match not just the direction of a decision but the justification behind it.

### Output Schema

```json
{
  "classifications": [
    {
      "decision_index": 1,
      "decision_quote": "...",
      "investment_signal": {"direction": "active|selective|conservative|no_signal", "reasoning": "..."},
      "positional_signal": {"direction": "advancing|measured|yielding|no_signal", "reasoning": "..."}
    }
  ],
  "investment_orientation": {
    "category": "invest_light",
    "description": "This user leans toward investing on decisions with clear impact. They shop significant renewals, close coverage gaps flagged by experts, and fix problems fully when customers are affected. On lower-stakes items — minor process improvements, optional add-ons with marginal benefit — they tend to accept the default. Their reasoning is primarily cost-benefit driven; they invest when the ROI is clear and conserve when it's ambiguous.",
    "confidence": "high",
    "supporting_decisions": 14
  },
  "positional_stance": {
    "category": "advance_light",
    "description": "This user holds ground on matters within their expertise and negotiates on high-stakes asks. They push back on timeline requests, counter-propose rather than accepting terms at face value, and independently evaluate recommendations before committing. On topics outside their domain or low-stakes interactions, they tend to follow expert guidance and accommodate.",
    "confidence": "high",
    "supporting_decisions": 8
  }
}
```

### Minimum Thresholds (Per Trait)

- Fewer than 8 signals → trait is NULL (partial profile)
- 8–14 signals → low confidence
- 15+ signals → high confidence

Thresholds are per-trait because signal distribution is typically asymmetric. A user may produce 16 investment decisions and only 4 positional decisions. That user gets high-confidence Investment Orientation and no Positional Stance — not a compromised read on both.

### Decision Moment Cap

Decision moments are capped at 50 most recent. A high-volume user (CFO, deal principal) with 80+ decision-bearing emails would otherwise push the Sonnet input to 4,000-6,000 tokens for decisions alone. The marginal value of decision #51-80 is low and the cost is linear. 50 provides sufficient signal for confident categorical assignment while keeping cost predictable. "Most recent" is preferred over random because recent decisions better represent current preferences (aligned with re-profiling rationale).

---

## Storage

### Database Schema

Migration: `supabase/migrations/031_preference_profile.sql`

```sql
-- Add preference profile column to profiles
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS preference_profile jsonb DEFAULT NULL;

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS preference_profiled_at timestamptz DEFAULT NULL;
```

Using JSONB rather than individual columns because:

- The schema may evolve (additional traits, metadata) without requiring migrations
- The profile is always read and written as a unit
- The descriptions are variable-length text that doesn't benefit from columnar storage

### Null Handling

Null handling operates at three levels:

1. **Column-level NULL** — `preference_profile` is NULL (user hasn't completed onboarding, or completed without enough decision-bearing emails in either trait). The draft generation prompt omits the preference section entirely. The model falls back to its current behavior — domain heuristics and `[USER TO CONFIRM]` where it can't infer direction.

2. **Trait-level NULL** — `preference_profile` exists but one trait is NULL (e.g., `positional_stance: null`). The prompt includes only the scored trait. Decisions that the missing trait would have resolved fall back to the model's current behavior for that decision type.

3. **Both traits scored** — Full preference section injected into the prompt.

All three states are graceful degradation, not failures. A partial profile with one strong trait is strictly better than no profile at all.

---

## Draft Generation Prompt Integration

The preference section is injected after the behavioral profile and before the email context:

```
PREFERENCE PROFILE:

Investment Orientation: INVEST LIGHT
This user leans toward investing on decisions with clear impact.
They shop significant renewals, close coverage gaps flagged by
experts, and fix problems fully when customers are affected. On
lower-stakes items, they tend to accept the default. Their
reasoning is primarily cost-benefit driven.

Positional Stance: ADVANCE LIGHT
This user holds ground on matters within their expertise and
negotiates on high-stakes asks. They push back on timeline
requests and counter-propose rather than accepting terms. On
topics outside their domain, they tend to follow expert guidance.

Use these traits to determine the DIRECTION of decisions in the
draft:
- Preference = WHAT to decide
- Behavior = HOW to express the decision
- Style = HOW to write it

When the two traits point in different directions on a given
decision:
1. The trait with the stronger (heavier) category takes priority
2. If equally weighted, the trait with higher confidence wins
3. If confidence is also equal, take the more cautious direction
   and mark as [USER TO CONFIRM]

[USER TO CONFIRM] is for missing factual information that cannot
be inferred from context or the preference profile. It is NOT for
decisions, preferences, or strategic choices.
```

**Confidence modulation:** For low-confidence traits, append to the description: "Based on limited data. Lean toward this direction but use [USER TO CONFIRM] for high-stakes decisions where the signal alone is insufficient to commit." For high-confidence traits, commit fully.

**Partial profiles:** Only the scored trait is injected. The instruction block references only the available trait.

---

## Cascade Integration

| Layer | Gate | Threshold | Action | Location |
|-------|------|-----------|--------|----------|
| 1 | Raw data | No separate gate — decoupled from `skip_guides` | Preference synthesis runs as long as Haiku extraction ran | `runner.py` — preference synthesis in both branches |
| 2 | Extraction | Fewer than 8 decisions (excluding `no_signal`) for a trait | That trait is NULL (partial profile) | `prompts.py` — Sonnet prompt returns null per-trait |
| 3 | Soft prompt | 8–14 signals for a trait | Low-confidence qualifier in description | `prompts.py` — Sonnet prompt rules |

Layer 1 is intentionally decoupled from `skip_guides`. The guide skip threshold (< 30 sent) exists for style/behavioral stratified bucketing. Preference extraction has a different data requirement — 8 decision-bearing emails per trait — and can succeed with fewer total sent emails. Haiku extraction always runs (it's outside the `skip_guides` branch), so decision moments are available regardless.

Layer 2 is enforced by the Sonnet prompt: "If a trait has fewer than 8 supporting decisions (excluding no_signal), return null for that trait." The synthesis function validates the response and passes through nulls. The `no_signal` exclusion is critical — decisions that don't bear on a trait should not inflate the count.

Layer 3 is enforced by the Sonnet prompt: "If a trait has 8-14 supporting decisions, set confidence to 'low' and append qualifier to the description." The draft generation prompt then modulates commitment based on the confidence field.

---

## File-by-File Changes

### 1. `worker/onboarding/prompts.py`

#### A. Extend `HAIKU_BEHAVIORAL_EXTRACTION_PROMPT`

Add two decision detection fields to the existing per-email JSON output schema. These sit alongside the existing `decision_type`, `commitment_pattern`, etc. fields. Haiku detects and extracts only — all interpretation is deferred to Sonnet.

**Add to the field definitions section (~lines 274-308):**

```python
# Decision detection fields (added for preference extraction)
# - contains_decision: boolean — does this email contain a decision moment
#   where the sender chose a course of action? Informational, scheduling,
#   and forwarding emails are NOT decisions. A decision requires the sender
#   to pick between alternatives or commit to an action.
# - decision_quote: string — if contains_decision is true, the verbatim
#   excerpt (1-2 sentences max) from the sent email where the decision is
#   expressed. If false, null. Do NOT summarize or interpret — extract the
#   exact words the sender used.
```

**Add to the JSON output example:**

```json
{
  "email_index": 1,
  "contact_type": "external_vendor",
  "decision_type": "decides",
  "response_completeness": "full",
  "commitment_pattern": "firm",
  "scope_behavior": "contained",
  "contains_decision": true,
  "decision_quote": "Let's go ahead and renew at the current rate — I don't think we need to shop this around."
}
```

#### B. Add `SONNET_PREFERENCE_SYNTHESIS_PROMPT`

New constant. Full prompt text:

```python
SONNET_PREFERENCE_SYNTHESIS_PROMPT = """You are analyzing a user's decision-making patterns extracted from their sent emails. Your task is to classify each decision along two personality dimensions and then synthesize a categorical profile.

## The Two Traits

### Investment Orientation
Core question: When something is suboptimal, broken, risky, or could be improved — does this user invest to address it?

### Positional Stance
Core question: When this user's interests intersect with another party's — or when an expert recommends a course of action — does the user yield or advance?

## Step 1: Per-Decision Classification

For each decision moment, classify the signal it provides on each trait.

Investment Orientation — classify as one of:
- "active": The user invested — shopped alternatives, closed a gap, fixed fully, investigated root cause, acted preemptively, explored options
- "selective": The user invested, but only after weighing cost-benefit or confirming the stakes justified it — qualified investment, conditional commitment
- "conservative": The user chose NOT to invest — accepted the status quo, took the partial fix, skipped the optional protection, waited rather than acted
- "no_signal": This decision does not provide evidence on investment orientation (e.g., a purely positional negotiation move)

Positional Stance — classify as one of:
- "advancing": The user pushed — negotiated, demanded reciprocity, exploited leverage, escalated, challenged the recommended path
- "measured": The user pushed, but selectively — held ground on the key issue while accommodating on lesser points, evaluated before following guidance
- "yielding": The user accommodated — conceded, followed expert guidance without evaluation, walked away rather than escalated, accepted the proposed terms
- "no_signal": This decision does not provide evidence on positional stance (e.g., a purely investment-oriented decision with no counterparty or expert)

IMPORTANT: "no_signal" means this decision provides NO evidence for that trait. It is NOT a middle ground or moderate position. Exclude no_signal decisions from the count for that trait.

## Step 2: Category Synthesis

After classifying all decisions, assign a category for each trait based on the pattern across the full decision set.

There is no middle option. Pick a side.

### Investment Orientation Categories

INVEST HEAVY: Dominant pattern is "active" signals across multiple decision types, including low-stakes decisions where most people would accept good-enough. Few or no "conservative" signals.

INVEST LIGHT: Majority of signals are "active" or "selective." The user invests on important decisions but exercises judgment on lower-stakes items. Distinguished from Invest Heavy by the presence of "selective" or "conservative" signals on lower-stakes decisions.

CONSERVE LIGHT: Majority of signals are "conservative." The user defaults to the status quo but invests when urgency or obvious cost of inaction demands it. Distinguished from Conserve Heavy by the presence of "active" signals on high-stakes decisions.

CONSERVE HEAVY: Dominant pattern is "conservative" signals across multiple decision types, including situations where investing would clearly be the better play. Few or no "active" signals.

### Positional Stance Categories

ADVANCE HEAVY: Dominant pattern is "advancing" signals across multiple interaction types, including low-stakes situations. Few or no "yielding" signals.

ADVANCE LIGHT: Majority of signals are "advancing" or "measured." The user pushes on significant matters but accommodates on lesser points. Distinguished from Advance Heavy by the presence of "measured" or "yielding" signals on lower-stakes interactions.

YIELD LIGHT: Majority of signals are "yielding." The user defaults to accommodation but holds ground when the cost of yielding is obvious. Distinguished from Yield Heavy by the presence of "advancing" signals on high-stakes matters.

YIELD HEAVY: Dominant pattern is "yielding" signals across multiple interaction types, including situations where pushing back would clearly be beneficial. Few or no "advancing" signals.

OBSERVATION BIAS NOTE:
Sent emails are biased toward action — decisions where the user chose NOT to act often don't generate a sent email. Ask: "what is this user's default when they have a genuine choice?" not "what is the ratio of active to conservative in the data?"

## Rules
- A single decision may provide signal for one or both traits.
- Some decisions may be ambiguous — classify the stronger signal only, set the weaker to no_signal.
- If a trait has fewer than 8 supporting decisions (excluding no_signal), return null for that trait.
- If a trait has 8-14 supporting decisions, set confidence to "low" and append to the description: "Based on limited data ({N} decisions). This profile may shift as more email history becomes available."
- If a trait has 15+ supporting decisions, set confidence to "high".
- Write descriptions in the voice of an observer describing a person, not as rules or instructions.
- The description should capture HOW this user reasons about decisions — do they lead with cost-benefit analysis, relationship impact, risk mitigation, or speed? This reasoning lens emerges from the decision quotes and helps the draft model match the justification, not just the direction.

## Output Format (JSON)

{
  "classifications": [
    {
      "decision_index": 1,
      "decision_quote": "...",
      "investment_signal": {"direction": "active|selective|conservative|no_signal", "reasoning": "..."},
      "positional_signal": {"direction": "advancing|measured|yielding|no_signal", "reasoning": "..."}
    }
  ],
  "investment_orientation": {
    "category": "invest_heavy|invest_light|conserve_light|conserve_heavy",
    "description": "...",
    "confidence": "high|low",
    "supporting_decisions": 14
  },
  "positional_stance": {
    "category": "advance_heavy|advance_light|yield_light|yield_heavy",
    "description": "...",
    "confidence": "high|low",
    "supporting_decisions": 8
  }
}

If a trait has fewer than 8 supporting decisions (excluding no_signal), set that trait's object to null instead.

## Contact Context
{contact_context}

## Decision Moments
{decisions_json}
"""
```

---

### 2. `worker/onboarding/extraction.py`

#### Modify `extract_behavioral_features()` (~line 239)

**Current return structure:**
```python
{
    "behavioral_features": [...],
    "sample_count": int,
    "usage": {...}
}
```

**New return structure — add `decision_moments` key:**
```python
{
    "behavioral_features": [...],
    "decision_moments": [...],   # NEW
    "sample_count": int,
    "usage": {...}
}
```

**Changes within the function:**

1. After parsing each Haiku batch response, separate the decision detection fields from the behavioral fields:

```python
# After parsing batch response (existing loop ~line 305-315)
for item in batch_result:
    # Existing: append behavioral fields
    behavioral_features.append({
        "decision_type": item.get("decision_type"),
        "commitment_pattern": item.get("commitment_pattern"),
        # ... existing fields
    })

    # NEW: extract decision moments (raw quote only, Sonnet interprets)
    # received_at is included so synthesize_preferences() can sort
    # chronologically before capping at 50 — batch processing order
    # is not guaranteed to be chronological.
    if item.get("contains_decision") and item.get("decision_quote"):
        decision_moments.append({
            "decision_quote": item.get("decision_quote"),
            "contact_type": item.get("contact_type"),
            "received_at": item.get("received_at"),
        })
```

2. Initialize `decision_moments = []` at the top of the function.

3. Include `decision_moments` in the return dict.

**Note on `received_at`:** This field is NOT extracted by Haiku — it comes from the email row metadata already available in the extraction loop context (the same data used to build the Haiku batch input). It's passed through to `decision_moments` so `synthesize_preferences()` can sort chronologically before capping at 50. If `received_at` is unavailable for a given email (edge case), it defaults to empty string in the sort, pushing it to the front (oldest position) — safe because the worst case is including an undated decision in the cap rather than excluding a dated one.

**No changes to:**
- `sample_unified_sent_emails()` — same sample
- `_prepare_behavioral_batches()` — same batching
- Batch size — stays at 10
- Error handling — decision fields are optional; if Haiku omits them, the email is simply not a decision moment

---

### 3. `worker/onboarding/synthesis.py`

#### Add `synthesize_preferences()` function

Follows the same pattern as `synthesize_behavioral_profile()` (lines 184-257).

```python
MAX_DECISION_MOMENTS = 50

def synthesize_preferences(decision_moments, contact_profiles):
    """Phase 4C-4: Classify decision moments and synthesize preference profile.

    Args:
        decision_moments: list of dicts from behavioral extraction
            [{decision_quote, contact_type, received_at}, ...]
        contact_profiles: enriched contact profiles from Phase 4A

    Returns:
        (preference_profile: dict or None, usage: dict)
        preference_profile contains investment_orientation and/or
        positional_stance (either can be null for partial profiles).
        Returns (None, usage) if no decision moments provided.
    """
    if not decision_moments:
        return None, {}

    # Sort by received_at to guarantee chronological order before capping.
    # Batch processing order from _prepare_behavioral_batches() is NOT
    # guaranteed chronological — it depends on batching strategy (could
    # group by contact type, body length, etc.). Explicit sort makes
    # the "most recent 50" guarantee independent of upstream ordering.
    decision_moments = sorted(
        decision_moments,
        key=lambda dm: dm.get("received_at") or "",
    )

    # Cap at 50 most recent decision moments. The marginal value of
    # decision #51-80 is low, and token cost is linear. "Most recent"
    # is preferred over random because recent decisions better represent
    # current preferences (aligned with re-profiling rationale).
    if len(decision_moments) > MAX_DECISION_MOMENTS:
        decision_moments = decision_moments[-MAX_DECISION_MOMENTS:]

    # Format contact context — duplicated from behavioral synthesis
    # (lines 212-227) intentionally. Extracting a shared helper would
    # modify synthesize_behavioral_profile(), risking regression in a
    # working code path. Refactor to shared helper in a separate commit
    # after this feature lands and passes tests.
    contact_lines = []
    for cp in (contact_profiles or []):
        if cp.get("relationship_significance") in ("critical", "high"):
            contact_lines.append(
                f"- {cp.get('email', 'unknown')}: "
                f"{cp.get('inferred_role', 'unknown role')} at "
                f"{cp.get('inferred_organization', 'unknown org')} "
                f"({cp.get('contact_type', 'unknown type')})"
            )
    contact_context = "\n".join(contact_lines) if contact_lines else "No high-significance contacts available."

    # Format decision moments as JSON
    decisions_json = json.dumps([
        {"decision_index": i + 1, **dm}
        for i, dm in enumerate(decision_moments)
    ], indent=2)

    # Build prompt
    prompt = SONNET_PREFERENCE_SYNTHESIS_PROMPT.format(
        contact_context=contact_context,
        decisions_json=decisions_json,
    )

    # Call Sonnet
    response, usage = call_sonnet(prompt, temperature=0.3, max_tokens=8192)

    # Parse response
    result = json.loads(clean_json_response(response))

    # Extract profile (either trait can be null — partial profiles supported)
    profile = {}
    io = result.get("investment_orientation")
    ps = result.get("positional_stance")

    # Validate category values before accepting
    valid_io = {"invest_heavy", "invest_light", "conserve_light", "conserve_heavy"}
    valid_ps = {"advance_heavy", "advance_light", "yield_light", "yield_heavy"}

    if io and io.get("category") in valid_io:
        profile["investment_orientation"] = io
    else:
        profile["investment_orientation"] = None

    if ps and ps.get("category") in valid_ps:
        profile["positional_stance"] = ps
    else:
        profile["positional_stance"] = None

    # If both traits are null, return None (no usable profile)
    if profile["investment_orientation"] is None and profile["positional_stance"] is None:
        return None, usage

    return profile, usage
```

**Contact formatting is intentionally duplicated.** The behavioral synthesis formats contact profiles for reconciliation context (lines 212-227). Rather than extracting a shared helper — which would modify `synthesize_behavioral_profile()` and risk regression in a working code path — the formatting logic is duplicated in `synthesize_preferences()`. Refactor to a shared helper in a separate commit after this feature lands and all existing tests pass.

---

### 4. `worker/onboarding/runner.py`

#### A. Pass decision_moments through from Haiku results

After awaiting the behavioral extraction future (~line 225):

```python
# Existing (~line 225)
behavioral_result = f_behavioral.result()

# NEW: extract decision moments for preference synthesis
decision_moments = []
if behavioral_result:
    decision_moments = behavioral_result.get("decision_moments", [])
```

#### B. Add preference synthesis — decoupled from `skip_guides`

Preference synthesis runs regardless of `skip_guides`. The guide skip threshold (< 30 sent emails) exists because style and behavioral synthesis need stratified bucketing across 6 categories. Preference extraction has a different data requirement — it needs 8 decision-bearing emails per trait, not 30 stratified emails. A user with 20 sent emails might have 8-10 decisions, which is enough for a low-confidence preference profile even though it's not enough for style/behavioral guides.

The gating is handled entirely by the Sonnet prompt (returns null for traits with < 8 signals) and the synthesis function (returns None if both traits are null). No separate gate needed in the runner.

**In the `else` branch of `skip_guides` (~lines 295-319), add a fourth future:**

```python
with ThreadPoolExecutor(max_workers=4) as executor:  # was 3
    f_topics = executor.submit(synthesize_topics, keyword_freqs)
    f_guide = executor.submit(synthesize_style_guide, style_features, contact_profiles)
    f_behavioral = executor.submit(synthesize_behavioral_profile, behavioral_features, contact_profiles)
    f_preferences = executor.submit(synthesize_preferences, decision_moments, contact_profiles)  # NEW

    topics_result, topics_usage = f_topics.result()
    guide_result, guide_usage = f_guide.result()
    behavioral_profile, behavioral_usage = f_behavioral.result()
    preference_profile, preference_usage = f_preferences.result()  # NEW
```

**In the `if skip_guides` branch (~lines 287-293), still run preference synthesis:**

```python
if skip_guides:
    # Existing: only synthesize topics
    topics_result, topics_usage = synthesize_topics(keyword_freqs)
    guide_result = None
    behavioral_profile = None

    # NEW: preference synthesis runs even when guides are skipped,
    # because its data requirement (8 decisions per trait) is independent
    # of the style/behavioral stratified sampling requirement (30 sent emails).
    # Haiku extraction still ran (it's outside the skip_guides branch),
    # so decision_moments are available.
    preference_profile, preference_usage = synthesize_preferences(
        decision_moments, contact_profiles
    )
```

Merge `preference_usage` into the Sonnet usage accumulator in both branches.

#### C. Persist preference profile

After the existing style/behavioral persistence block (~lines 349-374):

```python
# NEW: persist preference profile (~after line 374)
if preference_profile:
    db.update_preference_profile(user_id, preference_profile)

    io = preference_profile.get("investment_orientation")
    ps = preference_profile.get("positional_stance")
    logger.info(
        f"Preference profile saved: "
        f"investment={io['category'] if io else 'null'}, "
        f"positional={ps['category'] if ps else 'null'}"
    )
```

#### D. Do NOT add preference to `missing_components`

A missing preference profile should not trigger `complete_partial`. The preference layer is additive — the system worked without it. The prompt template already handles NULL by omitting the preference section. No changes to lines 342-346 or 400-411.

---

### 5. `worker/supabase_client.py`

#### Add `update_preference_profile()` method

Follows the same pattern as `update_behavioral_profile()` (lines 567-577):

```python
def update_preference_profile(self, user_id, preference_profile):
    """Persist synthesized preference profile to profiles table."""
    self.client.table("profiles").update({
        "preference_profile": json.dumps(preference_profile),
        "preference_profiled_at": datetime.utcnow().isoformat(),
    }).eq("id", user_id).execute()
```

---

## Ordering Constraints

| Constraint | How Enforced | Risk |
|------------|-------------|------|
| Haiku must complete before Sonnet synthesis | Existing — runner awaits all Haiku futures before entering synthesis block | None — no change |
| Phase 4A (contacts) must complete before 4C-4 | Existing — contact synthesis is sequential, completes before ThreadPoolExecutor block | None — no change |
| 4C-4 is independent of 4C-2 and 4C-3 | By design — preference synthesis uses decision_moments + contact_profiles, not style/behavioral features | None |
| Decision moments come from behavioral extraction | New — 4C-4 input depends on 4C-1b output | Enforced by pipeline ordering (Haiku completes → Sonnet starts) |

No new ordering constraints are introduced. The preference synthesis slots into the existing parallel Sonnet block with the same dependency pattern as the other synthesis functions.

---

## Error Handling

**Haiku extraction fails:** If the behavioral extraction future throws an exception, `decision_moments` defaults to `[]`. Preference synthesis receives an empty list and returns `(None, {})`. No preference profile is saved. Onboarding continues — this is the same graceful degradation pattern used for style/behavioral failures.

**Sonnet synthesis fails:** If `synthesize_preferences()` throws, catch the exception in the runner (same pattern as existing synthesis error handling), log it, set `preference_profile = None`. No preference profile is saved. Onboarding completes normally.

**Partial Haiku response:** If Haiku returns some emails without the decision detection fields (omits `contains_decision`), those emails are simply not added to `decision_moments`. The extraction function treats missing fields as "not a decision" — safe default.

**Sonnet returns malformed JSON:** The `clean_json_response()` utility and `json.loads()` will raise. Caught by the existing try/except in the runner. Preference profile set to None.

---

## What Does NOT Change

- Extension code — no changes
- Dashboard code — no changes
- Sync gate logic (`initial_sync_complete`) — no changes
- Status transitions — no new statuses added
- `complete_partial` determination — preference is not a required component
- Phase 3 (email feature extraction) — unchanged
- Phase 4C-1 (style extraction) — unchanged
- Phase 4A (contact synthesis) — unchanged
- Phase 4B (topic synthesis) — unchanged
- Phase 4C-2 (style guide synthesis) — unchanged
- Phase 4C-3 (behavioral profile synthesis) — unchanged
- Model training (Phase 7) — unchanged
- The unified 120-email sample — unchanged

---

## Token Cost Estimate

| Component | Additional Tokens | Frequency |
|-----------|------------------|-----------|
| Haiku: decision detection fields (output) | ~20-40 per email pair | ~120 emails / 10 per batch = 12 batches |
| Haiku total additional | ~240-480 output tokens | Per user onboarding |
| Sonnet: preference synthesis (input) | ~50-80 per decision moment × up to 50 decisions (capped) | 1 call per user |
| Sonnet: preference synthesis (output) | ~5,000-7,000 (classifications + categorical profile, max_tokens=8192) | 1 call per user |
| **Total additional per user** | **~7,500-11,000 tokens** | |

Total cost is marginal relative to the existing onboarding pipeline (style + behavioral extraction + 3 Sonnet synthesis calls).

---

## Implementation Sequence

### Step 1: Prompt Integration with Hardcoded Profiles

Validate the architecture before building extraction:

1. Add `preference_profile` JSONB column to profiles
2. Manually write preference profiles for each test configuration:
   - Invest Heavy + Advance Heavy
   - Conserve Light + Yield Light
   - Invest Heavy + Yield Light (mixed)
3. Inject into test user's profile, run drafts, evaluate differentiation

### Step 2: Validation

1. Run 10 extreme-configuration tests (5 emails × Invest Heavy/Advance Heavy + Conserve Light/Yield Light) with Style C + Behavior 1
2. Score on Preference Adherence: does the draft decide in the direction the profile indicates?
3. Re-run failure cases from original matrix (Test 40, Legal C4) with preference layer active
4. Go/no-go: if differentiation is weak, diagnose prompt phrasing before building extraction

### Step 3: Extraction Pipeline

Implement file-by-file changes described in the "File-by-File Changes" section above:

1. Extend Haiku extraction with `contains_decision` and `decision_quote` fields
2. Build Sonnet classification + synthesis with `synthesize_preferences()`
3. Wire into runner with `skip_guides` decoupling
4. Add `update_preference_profile()` to persistence layer
5. Run migration

### Step 4: Post-Launch Iteration

1. Log decision-direction edits (user flips a decision, not just rephrases) — signals preference profile misses
2. Track `[USER TO CONFIRM]` rate — sustained high rate after profiling indicates extraction quality issues
3. Monitor for temporal preference patterns (defer vs. front-load edits) — if consistent, evaluate adding a third trait for timing disposition
4. Re-profiling triggers: time-based (6–12 months), signal-based (decision-direction edit rate exceeds threshold), or manual (user-initiated)

---

## Testing Checklist

### Haiku Extraction (no regression)

- [ ] Run onboarding on a test user with >30 sent emails → verify decision_moments are extracted alongside existing behavioral_features
- [ ] Verify existing style + behavioral synthesis outputs are identical with and without the extraction.py changes (no regression in existing fields)
- [ ] Verify Haiku batch responses still parse correctly when decision fields are present
- [ ] Verify Haiku batch responses still parse correctly when decision fields are absent (older prompt cached, or Haiku omits optional fields)

### Sonnet Synthesis (categorical output)

- [ ] Verify full profile: feed Sonnet 20+ investment signals and 15+ positional signals → confirm both traits return valid categories (`invest_heavy|invest_light|conserve_light|conserve_heavy`, `advance_heavy|advance_light|yield_heavy|yield_light`)
- [ ] Verify partial profile: feed Sonnet 10 investment signals and 3 positional signals → confirm investment_orientation has a category, positional_stance is null
- [ ] Verify low-confidence qualifier: feed Sonnet 9 decisions for a trait → confirm description includes "Based on limited data" text and confidence is "low"
- [ ] Verify below-threshold: feed Sonnet 6 decisions for a trait → confirm that trait returns null (< 8 threshold)
- [ ] Verify category validation: if Sonnet returns an unexpected category value, synthesis function returns null for that trait (not a crash)

### Pipeline Integration

- [ ] Run onboarding on a test user with >30 sent emails → verify preference_profile is persisted as valid JSONB in profiles table with correct categories
- [ ] Run onboarding on a test user with <30 sent emails (skip_guides path) → verify preference synthesis still runs and produces a profile if enough decisions exist
- [ ] Verify onboarding completes as `complete` (not `complete_partial`) when preference profile is null
- [ ] Verify decision_moments are capped at 50 when a user has more than 50 decision-bearing emails

### End-to-End Draft Loop (bridges extraction → prompt integration)

- [ ] Write a known preference profile to DB (e.g., `invest_heavy` + `advance_light`), trigger draft generation on a test email, verify the prompt includes the preference section with correct category and description
- [ ] Verify draft output reflects the preference direction (e.g., `invest_heavy` user's draft invests rather than conserves)
- [ ] Verify NULL preference_profile → draft prompt omits the preference section entirely (no empty block, no placeholder)
- [ ] Verify partial profile (one trait null) → draft prompt includes only the scored trait
