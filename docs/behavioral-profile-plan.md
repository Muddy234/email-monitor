# Behavioral Profile — Implementation Plan (v3)

## Problem
The draft system captures *how the user writes* (style guide) but not *how the user thinks and acts*. Drafts sound right but behave wrong — they defer when the user decides, accept premises the user would challenge, and pad responses the user would keep terse.

## Solution
Add a **behavioral profile** parallel to the existing writing style guide. Extract behavioral features from sent emails (paired with their parent inbound messages) during onboarding → synthesize into a context-dependent behavioral profile via Sonnet → inject into the draft generation prompt with explicit priority over the style guide.

---

## Design Decisions

### Extraction requires thread context
A sent email alone is ambiguous. "Approved, go ahead" could mean the user decides quickly or is just rubber-stamping. Only by seeing the inbound ("Can we proceed with the transfer?") can Haiku infer decision-making behavior. The extraction prompt must receive **sent + parent pairs**, not sent emails alone.

**How we pair them:** The onboarding pipeline already computes sent ↔ received linkages in `_build_response_events()` (stats_extraction.py) using conversation_id → subject similarity → recipient+time matching. We reuse those pairings rather than recomputing them.

### Sampling strategy: by recipient diversity, not length
Style features converge across 50 emails because surface-level patterns are stable. Behavioral patterns are situational — the user might decide instantly with ops contacts but defer when legal is involved. Sampling must cover recipient/audience diversity:
- Stratify by **contact_type** (internal, external_legal, external_lender, etc.)
- Within each type, sample by **recency** (favor recent behavior)
- Target **80 pairs** (up from 50) to capture enough situational variety
- Fall back to length-stratified sampling if contact_type coverage is thin

### Split extraction schema: context-dependent vs. context-free
Not all behavioral fields need the parent message. Split into two tiers:

**Context-free fields** (observable from sent email alone):
- `pleasantry_level`: none | minimal | standard | warm
- `asks_clarifying_questions`: boolean

**Context-dependent fields** (require sent + parent pair):
- `decision_type`: decides | defers | delegates | asks_for_info (subsumes `uses_conditional_logic` — conditional decisions are a decision tactic)
- `challenges_premise`: boolean (subsumes skepticism — challenging is the observable action)
- `takes_action_in_reply`: boolean

All emails get context-free extraction. Only paired emails get the full schema.

### Four dimensions, not seven
The original 7-dimension schema spreads too thin for a 300-500 word profile. Consolidated to 4 high-signal dimensions with ~75-125 words each — enough room for multi-mode descriptions:

1. **Decision disposition** — decides / defers / delegates / gathers info. Absorbs conditional logic (a decision-making tactic, not a separate dimension).
2. **Information gap handling** — probes, challenges, accepts, asks questions. Absorbs skepticism (the observable behavior when something seems off).
3. **Action orientation** — takes action in reply (approves, rejects, instructs) vs. acknowledges and defers.
4. **Pleasantry calibration** — when warm, when terse, when straight to business.

**Response density** moves to the style guide where it belongs (it's a writing pattern, not a behavioral one).

### Privacy: no raw email addresses in extraction output
Replace `recipient_email` with `contact_type` at extraction time. Raw addresses never flow through the behavioral extraction → synthesis → LLM pipeline.

### Extraction-time contact_types are approximate
Behavioral extraction runs in parallel with Phase 4A (Sonnet contact synthesis), so real contact_types aren't available yet. We use a **domain-based heuristic** at extraction time (same org domain = internal, different = external). This will misclassify contacts who are external but functionally internal (partner entities, repeat outside counsel, etc.).

**Mitigation:** The synthesis prompt explicitly states that extraction-time contact_types are approximate domain-based labels, and that the authoritative contact profiles (with real types like `external_lender`, `external_legal`) are provided separately. Sonnet must reconcile by using the authoritative profiles to re-map features, not treat the two as different taxonomies.

### Prompt hierarchy: behavior governs action, style governs voice
The draft prompt explicitly states:
> The BEHAVIORAL PROFILE governs *what the reply does* — whether to decide, defer, probe, or instruct. The WRITING STYLE GUIDE governs *how it's written* — tone, vocabulary, sentence structure. When they conflict, behavioral profile wins.

### Synthesis must be context-aware, not flattened
The synthesis prompt instructs Sonnet to identify **where behavior varies by situation** rather than producing a single average. Output format should be mode-based:
- "With internal ops contacts: decides immediately, skips pleasantries"
- "With external legal: defers to partner, formal acknowledgment, asks for documentation"
- "When information is missing: probes before acting, asks clarifying questions"
- "When something seems wrong: flags the inconsistency directly"

### Handle inconsistent / unclear patterns
Not every user has clean behavioral modes. If the 80-email sample shows no clear pattern for a given dimension, Sonnet must explicitly state "no consistent pattern observed" rather than fabricating a generalization. The draft prompt knows to fall back to neutral behavior (acknowledge + provide clear next steps) when the profile is uncertain on a dimension.

### Feedback loop: draft edit tracking
Two signals, not just one:

1. **Edit distance ratio** (`edit_distance_ratio` on `drafts` table) — ratio of changed characters between AI draft and user-sent version. Known limitation: this is noisy. Character-level distance doesn't distinguish behavioral edits ("Let me check" → "Go ahead and approve") from cosmetic ones (paragraph reformatting). Requires volume before it's meaningful.

2. **Draft deleted flag** (`draft_deleted` boolean on `drafts` table) — whether the user discarded the draft entirely rather than editing it. A full delete is a much stronger signal than any edit distance — it means the draft was wrong enough to start over.

Neither signal is actionable at launch. They're instrumentation for future analysis.

### Staleness and re-profiling
Writing style is relatively timeless. Behavioral patterns shift with role changes, new deal phases, and new counterparties. Two mechanisms:

1. **Auto re-profile**: When `behavioral_profiled_at` is older than 90 days and the pipeline runs, automatically re-extract and re-synthesize. Piggybacks on existing pipeline runs — no new scheduler needed.

2. **Manual trigger**: Add a "Re-analyze my behavior" action in account settings that resets `behavioral_profiled_at` to null, triggering re-profiling on the next pipeline run.

---

## Changes Required

### 1. Haiku Extraction Prompt (Phase 4C-1b)

**File:** `worker/onboarding/prompts.py`

Add `HAIKU_BEHAVIORAL_EXTRACTION_PROMPT`. Input format per email:

```
--- PAIR N ---
INBOUND (from {sender_name}, contact_type: {type}):
{parent_body — up to 1500 chars}

USER'S REPLY:
{sent_body}
```

For unpaired sent emails (no matching parent), use a reduced format:

```
--- EMAIL N ---
SENT TO: contact_type: {type}
{sent_body}
```

Extraction schema:

```json
{
    "email_index": 1,
    "contact_type": "internal_colleague",
    "decision_type": "decides | defers | delegates | asks_for_info | n/a",
    "challenges_premise": true/false/null,
    "asks_clarifying_questions": true/false,
    "pleasantry_level": "none | minimal | standard | warm",
    "takes_action_in_reply": true/false/null
}
```

Notes:
- `contact_type` is a domain-based approximation at extraction time (see design decision above)
- Fields marked `null` when context isn't available (unpaired emails)
- `n/a` for `decision_type` when the inbound wasn't requesting a decision
- Parent body truncated to **1500 chars** (not 500 — deal flow emails are long, and the behavioral signal often lives in the specifics)

### 2. Behavioral Feature Extraction Function

**File:** `worker/onboarding/extraction.py`

Add `extract_behavioral_features(sent_emails, response_events, received_emails, contact_type_map)`:

1. **Build pairing map**: Use `response_events` to link sent emails to their parent inbound via `conversation_id` + `_response_msg_id` (the existing linkage from stats_extraction)
2. **Build contact_type lookup**: Domain-based heuristic for extraction (same org = `internal`, different = `external`). Real contact_types applied at synthesis time.
3. **Sample 80 emails**: Stratify by contact_type first, then recency within each type. Ensure at least 3 emails per contact_type where available.
4. **Format pairs**: For each sampled sent email, include its paired parent (truncated to **1500 chars**) if available
5. **Batch and extract**: Groups of 10 (pairs are larger with 1500-char parents), 5 concurrent workers, Haiku with temperature=0
6. **No raw email addresses**: Use contact_type labels only

### 3. Sonnet Synthesis Prompt (Phase 4C-3)

**File:** `worker/onboarding/prompts.py`

Add `SONNET_BEHAVIORAL_PROFILE_PROMPT`:

```
Based on the following behavioral pattern analysis from a user's sent emails
(paired with the inbound messages they were replying to), generate a behavioral
profile (300-500 words) that another AI can follow to replicate this user's
decision-making patterns when drafting email replies.

IMPORTANT: The contact_type labels in the extraction data are APPROXIMATE
(domain-based: same org = internal, different org = external). The authoritative
contact profiles with specific types (external_lender, external_legal, etc.) are
provided separately below. Use the authoritative profiles to re-map and refine
the extraction data. Do not treat extraction-time labels and profile labels as
different taxonomies.

The profile MUST cover these 4 dimensions:

1. Decision disposition — Does the user decide, defer, delegate, or gather info?
   Describe EACH mode and the conditions that trigger it. Include whether the
   user gives conditional directives ("if X, do Y") as a decision tactic.
   e.g., "Decides immediately for operational requests from internal contacts.
   Defers to partner (Luke) for financial/legal decisions. Uses conditional
   instructions when the decision hinges on one unknown: 'If it was us, delete it.'"

2. Information gap handling — When information is missing or something seems off,
   does the user accept the premise, ask clarifying questions, or challenge it?
   Include skepticism patterns — does the user flag inconsistencies directly?
   e.g., "Probes before acting — asks 'who initiated this?' before approving.
   Calls out oddities bluntly: 'That's odd.'"

3. Action orientation — Does the user take action in the reply (approve, reject,
   instruct) or acknowledge and defer? Under what conditions?

4. Pleasantry calibration — When does the user skip thank-yous and get straight
   to business? When warm? Break down by contact_type if patterns differ.

CRITICAL INSTRUCTIONS:
- Do NOT produce a single flattened average. Behavior varies by context.
- Identify MODES and label what triggers each mode (contact_type, situation type,
  information completeness).
- If no clear pattern exists for a dimension, state "no consistent pattern
  observed" rather than guessing. The draft generator will fall back to neutral
  behavior for uncertain dimensions.
- Use concrete examples from the data to anchor each pattern.
- If a pattern only appears with certain contact_types, say so explicitly.
- This profile will be injected alongside a separate WRITING STYLE GUIDE. This
  profile governs WHAT the user does (decisions, actions, questions). The style
  guide governs HOW they write (tone, vocabulary, structure). Do not repeat
  style information here.

Output as plain text, not JSON.
```

### 4. Synthesis Function

**File:** `worker/onboarding/synthesis.py`

Add `synthesize_behavioral_profile(behavioral_features, contact_profiles)`:
- Mirrors `synthesize_style_guide()` structure
- Passes both the extraction features AND the authoritative contact profiles list to Sonnet so it can reconcile contact_types
- Calls Sonnet with `SONNET_BEHAVIORAL_PROFILE_PROMPT`
- Returns plain text behavioral profile, or None on failure

### 5. Onboarding Pipeline Integration

**File:** `worker/onboarding/runner.py`

Phase 3 + 4C-1 block (~line 200): Add behavioral extraction as a third parallel Haiku task:

```
ThreadPoolExecutor (3 workers):
  - extract_email_features(received, contact_freq)        # existing
  - extract_writing_styles(sent)                           # existing
  - extract_behavioral_features(sent, response_events,     # NEW
      received, domain_contact_type_map)
```

Uses domain-based heuristic for contact_type at extraction time (see design decision). `response_events` is already computed in Phase 2 and available.

Phase 4B + 4C-2 block (~line 228): Add behavioral synthesis as a third parallel Sonnet task:

```
ThreadPoolExecutor (3 workers):
  - synthesize_topics(keywords)                            # existing
  - synthesize_style_guide(style_features, contacts)       # existing
  - synthesize_behavioral_profile(behavioral_features,     # NEW
      contact_profiles)  ← authoritative contact_types from Phase 4A
```

Store via `db.update_behavioral_profile(user_id, behavioral_profile)` (~line 258).

**Auto re-profiling**: In the main pipeline loop (`run_pipeline.py`), check if `behavioral_profiled_at` is older than 90 days. If so, re-run behavioral extraction + synthesis using the same onboarding functions, writing the result back to `profiles.behavioral_profile`.

### 6. Database Schema

**File:** `supabase/migrations/017_behavioral_profile.sql`

```sql
-- Behavioral profile on user profiles
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS behavioral_profile text,
  ADD COLUMN IF NOT EXISTS behavioral_profiled_at timestamptz;

-- Feedback signals on drafts
ALTER TABLE public.drafts
  ADD COLUMN IF NOT EXISTS edit_distance_ratio real,
  ADD COLUMN IF NOT EXISTS draft_deleted boolean DEFAULT false;

COMMENT ON COLUMN public.drafts.edit_distance_ratio IS
  'Ratio of characters changed between AI draft and user-sent version. Noisy metric — requires volume.';
COMMENT ON COLUMN public.drafts.draft_deleted IS
  'True if user discarded the draft entirely rather than editing. Stronger signal than edit distance.';
```

### 7. Supabase Client

**File:** `worker/supabase_client.py`

Add `update_behavioral_profile(user_id, profile_text)`:

```python
def update_behavioral_profile(self, user_id, profile_text):
    self.client.table("profiles").update({
        "behavioral_profile": profile_text,
        "behavioral_profiled_at": datetime.utcnow().isoformat(),
    }).eq("id", user_id).execute()
```

### 8. Draft Prompt Integration (Python)

**File:** `worker/pipeline/prompts.py`

Update `DEFAULT_DRAFT_PROMPT_TEMPLATE`:

Add to `<thinking>` steps (after step 6):
```
7. Behavioral alignment — Review the BEHAVIORAL PROFILE if provided. How would
   the user approach this situation? Would they decide, defer, probe, or
   instruct? Would they challenge any premises? Would they skip pleasantries?
   Match the user's behavioral mode for this contact type and situation. If the
   profile says "no consistent pattern observed" for a dimension, default to
   neutral behavior (acknowledge the message and provide clear next steps).
```

Add to output instructions:
```
- The BEHAVIORAL PROFILE governs WHAT the reply does — whether to decide, defer,
  probe, challenge, or instruct. The WRITING STYLE GUIDE governs HOW it's
  written — tone, vocabulary, sentence structure. When they conflict, the
  behavioral profile takes priority.
- If the behavioral profile indicates the user would probe or ask clarifying
  questions in this type of situation, the draft MUST ask those questions rather
  than deferring or accepting the premise.
- If the behavioral profile says "no consistent pattern" for a dimension, fall
  back to neutral: acknowledge the message, provide clear next steps, and use
  [USER TO CONFIRM] for decisions you cannot make.
```

**File:** `worker/pipeline/drafts.py`

Add behavioral_block injection (~line 89, after style_block):

```python
behavioral_profile = action_context.get("behavioral_profile", "")
behavioral_block = ""
if behavioral_profile:
    behavioral_block = f"\n\nBEHAVIORAL PROFILE:\n{behavioral_profile}\n"
```

Insert `{behavioral_block}` into prompt assembly between `{style_block}` and `{thread_block}`.

**File:** `worker/run_pipeline.py` (~line 926)

```python
behavioral_profile = profile.get("behavioral_profile") or ""
if behavioral_profile:
    action_context["behavioral_profile"] = behavioral_profile
```

### 9. Draft Prompt Integration (JS — Frontend Draft Tester)

**File:** `web/js/devtools/prompt-builder.js`

- Update `buildSystemPrompt()`: Add thinking step 7 + priority hierarchy + neutral fallback
- Update `buildUserPrompt()`: Add `behavioralProfile` parameter, build `behavioralBlock`, inject into prompt

**File:** `web/js/devtools/draft-tester.js`

- Add `behavioral_profile` to the profile select query
- Pass as `behavioralProfile` to `buildUserPrompt()`

### 10. Re-profiling Trigger (Account Settings)

**File:** `web/js/pages/account.js` (or equivalent settings page)

Add a "Re-analyze my communication style" button that sets `behavioral_profiled_at = null` on the profile. The next pipeline run detects the null and re-runs behavioral extraction + synthesis.

**File:** `worker/run_pipeline.py`

In the pipeline entry point, check staleness:

```python
behavioral_at = profile.get("behavioral_profiled_at")
if behavioral_at:
    age_days = (datetime.utcnow() - parse(behavioral_at)).days
    if age_days > 90:
        behavioral_at = None  # trigger re-profiling

if not behavioral_at and profile.get("onboarding_status") == "complete":
    # Re-run behavioral extraction + synthesis
    ...
```

---

## Files Modified (Summary)

| File | Change |
|------|--------|
| `worker/onboarding/prompts.py` | Add `HAIKU_BEHAVIORAL_EXTRACTION_PROMPT` + `SONNET_BEHAVIORAL_PROFILE_PROMPT` |
| `worker/onboarding/extraction.py` | Add `extract_behavioral_features()` with sent+parent pairing, 1500-char truncation |
| `worker/onboarding/synthesis.py` | Add `synthesize_behavioral_profile()` with authoritative contact_type reconciliation |
| `worker/onboarding/runner.py` | Wire behavioral extraction + synthesis into parallel pipeline |
| `worker/supabase_client.py` | Add `update_behavioral_profile()` |
| `worker/pipeline/prompts.py` | Update system prompt: thinking step 7, priority hierarchy, neutral fallback |
| `worker/pipeline/drafts.py` | Inject `behavioral_block` into user prompt |
| `worker/run_pipeline.py` | Load behavioral_profile, add 90-day staleness check + re-profiling |
| `web/js/devtools/prompt-builder.js` | Mirror all prompt changes in JS |
| `web/js/devtools/draft-tester.js` | Fetch + pass behavioral profile |
| `web/js/pages/account.js` | Add re-analyze button |
| `supabase/migrations/017_behavioral_profile.sql` | Profile columns + draft feedback columns |

## Verification

1. **Migration**: Apply `017_behavioral_profile.sql` to add new columns
2. **Re-onboard**: Reset onboarding status for test user, run full pipeline
3. **Inspect extraction**: Check logs — verify:
   - Paired emails show all 5 fields populated
   - Unpaired emails show `null` for context-dependent fields
   - No raw email addresses in extraction output
   - Contact_types are domain-based approximations (internal/external)
4. **Inspect synthesis input**: Verify Sonnet receives both behavioral features (with approximate types) AND authoritative contact profiles (with specific types)
5. **Inspect profile**: Query `profiles.behavioral_profile` — verify:
   - Mode-based (not a flat average)
   - References specific contact_types from authoritative profiles
   - Covers all 4 dimensions
   - Says "no consistent pattern" where data is insufficient (not a fabricated generalization)
6. **Draft A/B test**: Use the devtools draft tester on the Michael pending-transfer email:
   - Without behavioral profile → expect the deferral-style draft
   - With behavioral profile → expect probing questions + conditional decision
7. **Staleness**: Set `behavioral_profiled_at` to 91 days ago, run pipeline, verify re-profiling triggers
8. **Feedback columns**: Create a draft, verify `edit_distance_ratio` and `draft_deleted` columns exist and accept writes
