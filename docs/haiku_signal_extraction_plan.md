# Haiku Signal Extraction — Prompt Architecture Plan

## Overview

Each inbound email is processed through a single Haiku call that returns a structured JSON object containing 5 classification signals plus direct priority, draft, and reasoning decisions. The goal is to give Haiku exactly enough context to classify accurately while keeping token count minimal for cost efficiency.

---

## Architectural Transition

This plan replaces the existing scoring infrastructure wholesale. The current `scorer.py` uses a multiplicative model with 11 regex-derived boolean lifts, per-user trained weights via `model_trainer.py`, isotonic calibration, and recurring pattern detection. All of that becomes unused.

The replacement: a single Haiku call that extracts 5 explanatory signals and makes the priority/draft decisions directly. No deterministic scoring formula, no hand-tuned weights, no multipliers. Haiku has the full email text, sender context, and thread metadata — it makes the holistic judgment because it has the full picture.

The existing `model_trainer.py` and `scoring_parameters` infrastructure can be deprecated. If per-user weight adaptation is needed later, the scoring weights can be stored per-user in the existing `scoring_parameters` table and adjusted via feedback. But that's a future concern — get the base signals right first.

### Pipeline Integration

The current pipeline runs 5 stages: filter → score (`scorer.py`) → enrich → classify (Haiku) → draft. The new signal extraction replaces both stage 2 (regex scoring) and stage 5 (Haiku classification) with a single Haiku call.

Old pipeline:
```
filter → score (regex, scorer.py) → enrich (contact lookup) → classify (Haiku) → draft (Sonnet)
  5 stages, 2 LLM calls (Haiku classify + Sonnet draft)
```

New pipeline:
```
filter → pre-process (regex strip + contact lookup) → extract (Haiku) → draft (Sonnet)
  4 stages, 2 LLM calls (Haiku signals + Sonnet draft) — same LLM cost, scorer.py eliminated
```

The enrichment step (old stage 3) folds into pre-processing — contact profile lookup now feeds the sender context line rather than being a separate pipeline stage. The old classify call's outputs map cleanly: `needs_response` → `draft == true`, `priority` → `pri`. The `action` and `context` fields from the old classify call were feeding the draft prompt — the `reason` field now serves this role. When Sonnet generates a draft, it receives the `reason` string alongside the email body — e.g., "Sender requests signed documents by Thursday EOD for a rate lock expiration with financial consequences" is essentially a draft brief that tells Sonnet what the response needs to accomplish. This is free context that makes drafts better without adding another LLM call.

### Draft Gating

Old gate: `needs_response == true` (from Haiku classify call).

New gate: `draft == true` (Haiku decides directly).

Haiku makes the holistic judgment on whether an email warrants a draft response. It considers the signals, the email content, the sender context, and the thread state — all in a single pass. No formula re-derives what Haiku already knows.

### Sender Tier Source

The `contacts` table currently has `contact_type` ("internal") but no concept of "critical" or "professional." The tier system is new and needs a source.

Launch implementation (zero user effort required):
1. **Domain lookup table** — A new `domain_tiers` table mapping known domains to tiers. Seed with lender domains (e.g., zionsbank.com → `C`), title company domains, investor domains, and legal counsel domains. This covers the highest-stakes senders on day one.
2. **Org domain match** — If sender domain matches the user's own org domain and no other tier is assigned, default to `I` (internal).
3. **Fallback logic** — If sender has prior email history in `contacts` but no tier assignment → `P` (professional). If no prior history → `U` (unknown).

**Pre-launch priority:** The `domain_tiers` seed list is the highest-leverage task before deployment. Everything else in this plan works automatically, but if the table is empty on day one, every sender starts at `P` or `U`, and Haiku's `pri` decisions lose their most important contextual input. Even 10-15 domains — active lenders, title companies, investor entities, legal counsel — would cover the majority of high-stakes inbound. This is a 20-minute task that meaningfully improves day-one accuracy.

Future enhancement: Add a `sender_tier_override` column to `contacts` for manual tagging. This is the escape valve for edge cases — a sender whose domain doesn't signal criticality but whose emails are high-stakes (e.g., a consultant on a personal Gmail).

### Schema Changes

Add signals and decisions to `response_events` rather than creating a new table.

New columns on `response_events`:
```sql
ALTER TABLE response_events ADD COLUMN mc boolean;
ALTER TABLE response_events ADD COLUMN ar boolean;
ALTER TABLE response_events ADD COLUMN ub boolean;
ALTER TABLE response_events ADD COLUMN dl boolean;
ALTER TABLE response_events ADD COLUMN rt text CHECK (rt IN ('none', 'ack', 'ans', 'act', 'dec'));
ALTER TABLE response_events ADD COLUMN pri text CHECK (pri IN ('high', 'med', 'low'));
ALTER TABLE response_events ADD COLUMN draft boolean;
ALTER TABLE response_events ADD COLUMN reason text;
ALTER TABLE response_events ADD COLUMN sender_tier text CHECK (sender_tier IN ('C', 'I', 'P', 'U'));
```

New table for domain-based tier lookups:
```sql
CREATE TABLE domain_tiers (
    domain text PRIMARY KEY,
    tier text CHECK (tier IN ('C', 'I', 'P', 'U')),
    source text DEFAULT 'seed',  -- 'seed', 'manual', 'derived'
    created_at timestamptz DEFAULT now()
);
```

Deprecation path for old columns: Keep `needs_response`, `priority`, `action`, `context`, and `project` populated during a transition window by mapping from the new signals (e.g., `needs_response = draft`). This prevents breaking any UI or downstream logic that still reads the old columns. Once the new pipeline is stable and all consumers are migrated, drop the old columns in a cleanup migration.

---

## What Gets Sent to Haiku

The prompt is composed of three blocks, assembled per-email at runtime.

### Block 1: System Prompt (~250 tokens, static, cached)

The system prompt is identical for every email and should be cached via Anthropic's prompt caching. Includes one few-shot example for format anchoring at near-zero marginal cost (cached).

```
Classify this email. Return JSON only, no other text.

Signals:
mc (bool): Financial/legal/deal consequence. Triggers: funding, closing, default, penalty, expiration, compliance, rate lock, guaranty, wire, lien, amendment, maturity.
ar (bool): Sender needs recipient to do something. Includes implicit asks and follow-ups.
ub (bool): Someone downstream is blocked waiting on recipient. Process paused pending their input.
dl (bool): Time constraint on a request. Explicit date/time or urgency language tied to an ask. Ignore dates as context only.
rt (enum none|ack|ans|act|dec): Expected response type. If multiple apply, pick highest: dec>act>ans>ack>none.

Decisions:
pri (enum high|med|low): Overall priority considering all signals, sender tier, and context.
draft (bool): Whether this email warrants a draft response. True for questions, actions, decisions. False for FYI, acknowledgments, no-response-needed.
reason (string): Why draft is true or false. Under 30 words. Also serves as the draft brief for response generation.

Context line format: sender|tier|thread_depth|unanswered
Tiers: C=critical, I=internal, P=professional, U=unknown

Example:
Input: Jane Smith jane@lender.com|C|1|false
S:Draw Request #4 — Approved
Your draw request has been approved and funds will wire Thursday. No action needed on your end.
Output: {"mc":true,"ar":false,"ub":false,"dl":false,"rt":"none","pri":"med","draft":false,"reason":"Informational notice of approved draw request. No action or response needed from recipient."}
```

The few-shot example's `reason` field sets the tone. Haiku will pattern-match on its style — the current example is clean and professional, which is the right register. If reasons drift toward verbosity or a different voice over time, the fix is in the example, not in adding more instructions.

### Block 2: Sender Context (~15 tokens, dynamic per-email)

Compressed into a single pipe-delimited line.

```
Tyler Mills tyler@aretecollective.com|I|2|true
```

### Block 3: Email Body (variable, ~300-800 tokens typical)

Pre-processed before insertion:

- **Reply-marker stripping.** Regex pre-pass removes everything below the first reply boundary.
- **Signature stripping.** Regex removes common signature patterns.
- **Header injection.** Subject line prepended as `S:[value]`.
- **Length cap.** Truncate to 1,000 tokens. If exceeded, take first 400 + last 400 with `[... truncated ...]` marker.

---

## Expected Output Format

```json
{"mc":true,"ar":true,"ub":false,"dl":true,"rt":"act","pri":"high","draft":true,"reason":"Sender requests signed documents by Thursday EOD for a rate lock expiration with financial consequences. Direct action needed from recipient."}
```

Roughly ~60-80 tokens output. The 5 signals provide transparency for the pipeline trace UI. The `pri`, `draft`, and `reason` fields are the actual decisions that drive the pipeline.

---

## Pre-Processing Pipeline (Before Haiku)

| Step | Method | Purpose |
|---|---|---|
| Auto-generated detection | Header check (`X-Auto-Submitted`, `List-Unsubscribe`, `Precedence: bulk`) | Skip Haiku entirely. No signal extraction needed. |
| Reply-marker stripping | Regex | Remove quoted thread history so Haiku only sees new content. |
| Signature stripping | Regex | Remove boilerplate signatures and disclaimers. |
| Sender domain blocklist | Lookup table | Known low-value domains (newsletters, noreply@) skip Haiku. |
| Thread metadata assembly | Supabase query | Pull thread depth and prior-unanswered flag from conversation history. |
| Contact profile lookup | Supabase query | Pull sender info from contacts table. |
| Sender tier resolution | Domain match + lookup | Resolution order: (1) `sender_tier_override` on contact if set, (2) `domain_tiers` table lookup, (3) org domain match → `I`, (4) prior email history exists → `P`, (5) fallback → `U`. |

Estimated skip rate from pre-filtering: 30-40% of inbound volume never hits Haiku.

---

## Post-Processing (After Haiku)

No scoring formula. Haiku's `draft` and `pri` decisions are used directly:

1. Parse JSON response.
2. Validate and coerce fields per-field (see below).
3. Persist all 8 fields + `sender_tier` to `response_events`.
4. Backfill deprecated columns: `needs_response = draft`, map `pri` to old priority scale.
5. If `draft == true` → pass email + `reason` string to Sonnet for draft generation. The `reason` field serves as the draft brief — it tells Sonnet what the response needs to accomplish.
6. If `draft == false` → pipeline complete for this email.

**Validation and coercion (step 2):**

Three failure modes exist, handled in order:

1. **Network error / no response** — Full fallback: all signals false, `pri = "low"`, `draft = false`, `reason = "Signal extraction failed"`. Log for monitoring.
2. **Malformed JSON (parse failure)** — Same full fallback. Log the raw response for debugging.
3. **Valid JSON with wrong types or missing keys** — Coerce per-field rather than rejecting the entire response. A partial signal set is better than no signals.

Per-field coercion rules:
- `mc`, `ar`, `ub`, `dl`, `draft`: if truthy string ("yes", "true", "1") → `true`. If missing → `false`.
- `rt`: if missing or invalid enum value → `"none"`.
- `pri`: if missing or invalid enum value → `"low"`.
- `reason`: if missing → `""`. If present, truncate to 200 chars as a safety cap.

---

## Token Budget & Cost Estimate

| Component | Tokens |
|---|---|
| System prompt (cached) | ~250 |
| Sender context | ~15 |
| Email body (avg) | ~500 |
| Output (signals + decisions + reason) | ~60-80 |
| **Total per email** | **~515 input + ~70 output** |

At Haiku pricing ($0.25/MTok input, $1.25/MTok output):

- Per email: ~$0.000129 input + $0.000088 output = **~$0.000217 per email**
- Power user (150 emails/day, 30% pre-filtered): ~105 emails hit Haiku → **~$0.68/month**

The `reason` field adds ~40-50 tokens to output vs. the no-reason version (~$0.21/month increase). Worthwhile for trace transparency and debugging signal quality.

### Net Pipeline Cost Comparison

Same number of Haiku calls as the old pipeline (one per email). The signal extraction call replaces the old classify call. Net LLM cost is comparable — slightly higher output tokens (reason field) offset by lower input tokens (compressed context). The Sonnet draft call is unchanged.

---

## Prompt Assembly (Pseudocode)

```python
SIGNAL_EXTRACTION_SYSTEM_PROMPT = """Classify this email. Return JSON only, no other text.

Signals:
mc (bool): Financial/legal/deal consequence. Triggers: funding, closing, default, penalty, expiration, compliance, rate lock, guaranty, wire, lien, amendment, maturity.
ar (bool): Sender needs recipient to do something. Includes implicit asks and follow-ups.
ub (bool): Someone downstream is blocked waiting on recipient. Process paused pending their input.
dl (bool): Time constraint on a request. Explicit date/time or urgency language tied to an ask. Ignore dates as context only.
rt (enum none|ack|ans|act|dec): Expected response type. If multiple apply, pick highest: dec>act>ans>ack>none.

Decisions:
pri (enum high|med|low): Overall priority considering all signals, sender tier, and context.
draft (bool): Whether this email warrants a draft response. True for questions, actions, decisions. False for FYI, acknowledgments, no-response-needed.
reason (string): Why draft is true or false. Under 30 words. Also serves as the draft brief for response generation.

Context line format: sender|tier|thread_depth|unanswered
Tiers: C=critical, I=internal, P=professional, U=unknown

Example:
Input: Jane Smith jane@lender.com|C|1|false
S:Draw Request #4 — Approved
Your draw request has been approved and funds will wire Thursday. No action needed on your end.
Output: {"mc":true,"ar":false,"ub":false,"dl":false,"rt":"none","pri":"med","draft":false,"reason":"Informational notice of approved draw request. No action or response needed from recipient."}"""


def resolve_sender_tier(sender_profile, user_domain, domain_tiers_cache):
    """Tier resolution: override > domain table > org match > history > unknown"""
    if sender_profile.tier_override:
        return sender_profile.tier_override
    if sender_profile.domain in domain_tiers_cache:
        return domain_tiers_cache[sender_profile.domain]
    if sender_profile.domain == user_domain:
        return "internal"
    if sender_profile.has_prior_history:
        return "professional"
    return "unknown"


def build_signal_prompt(email, sender_profile, thread_meta, user_domain, domain_tiers_cache):
    system = SIGNAL_EXTRACTION_SYSTEM_PROMPT

    tier_map = {"critical": "C", "internal": "I", "professional": "P", "unknown": "U"}
    tier = resolve_sender_tier(sender_profile, user_domain, domain_tiers_cache)

    sender_line = (
        f"{sender_profile.name} {sender_profile.email}"
        f"|{tier_map[tier]}"
        f"|{thread_meta.depth}"
        f"|{thread_meta.has_unanswered}"
    )

    clean_body = strip_reply_markers(email.body)
    clean_body = strip_signatures(clean_body)
    clean_body = truncate_smart(clean_body, max_tokens=1000)

    user_message = f"{sender_line}\nS:{email.subject}\n\n{clean_body}"

    return {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 150,
        "system": system,
        "messages": [{"role": "user", "content": user_message}]
    }
```

---

## Decisions (Resolved)

1. **Confidence scores** — Skipped. Revisit only if systematic misclassifications emerge.
2. **Batch API** — Not worth it at current volume. Revisit at scale.
3. **Few-shot example** — Yes, one example in cached system prompt. The example's `reason` field sets the tone — Haiku pattern-matches on its style. If reasons drift, fix the example.
4. **User feedback loop** — Deferred. Get base signals right first.
5. **Subject-line fast path** — Skipped. Not justified by savings.
6. **Pipeline integration** — Signal extraction replaces both `scorer.py` and old Haiku classify call.
7. **Draft gating** — `draft == true` from Haiku directly. No formula.
8. **Sender tier source** — Domain lookup table + org domain match + manual override (future). Seed list is highest-leverage pre-launch task.
9. **Schema** — Add columns to `response_events`. Deprecate old columns with backfill mapping.
10. **Deterministic scoring** — Eliminated. Haiku makes priority and draft decisions directly with full context. The 5 signals are explanatory metadata for the pipeline trace, not inputs to a formula.
11. **Reasoning** — Haiku provides a draft brief (under 30 words) explaining its decision. Stored in `reason` column on `response_events`. Double-duty: trace transparency + input to Sonnet draft prompt.
12. **Reason as draft brief** — The `reason` string is passed to Sonnet alongside the email body during draft generation. It tells Sonnet what the response needs to accomplish — free context that improves draft quality without an extra LLM call.
13. **Malformed JSON handling** — Three failure modes: network error (full fallback), parse failure (full fallback), valid JSON with wrong types/missing keys (per-field coercion). A partial signal set is better than no signals. Truthy strings coerce to bools; missing fields get safe defaults.
