# Draft Quality Refinement Plan

All changes are **backend-only** (Railway worker + Supabase edge functions) — safe to deploy while Chrome extension is under review.

---

## Phase 0: Evaluation Infrastructure
_Must come first — can't improve what you can't measure._

### 0A. Draft quality logging table
- **Create** `draft_evaluations` table in Supabase (user_id, draft_id, email_id, prompt_variant, thinking_text, generation_time_ms, token_usage, user_rating, user_notes)
- Store the chain-of-thought `thinking_text` that's currently extracted then discarded
- Enables reviewing model reasoning when drafts miss the mark
- **Files:** Supabase migration SQL, `worker/run_pipeline.py` (save thinking + timing)

### 0B. Prompt variant tracking
- Add `prompt_variant` string field to `drafts` table
- Tag each draft with the prompt version that generated it (e.g., `"v1.0"`, `"v1.1-brevity"`)
- Enables A/B comparison when iterating on prompts
- **Files:** Supabase migration SQL, `worker/pipeline/drafts.py` (pass variant tag through)

### 0C. Dashboard review panel
- Add a draft review section to the devtools panel: show draft + thinking side-by-side, quick rating buttons (good/too-verbose/wrong-tone/wrong-decision/missed-context)
- Ratings write to `draft_evaluations` table
- **Files:** `web/js/devtools/` (new panel or extend draft-tester.js)

---

## Phase 1: Classification Accuracy
_Fewer wrong drafts = less noise, more trust._

### 1A. Add CC + high-significance example to signal prompt
- The current 6 examples include one CC case (Example 6) but it's borderline. Add a clearer example where a high-significance CC sender warrants `draft=true` because the content is in the user's domain
- Reduces missed draft opportunities for CC'd emails from important senders
- **File:** `worker/pipeline/signal_extractor.py` — add Example 7 to `SIGNAL_EXTRACTION_SYSTEM_PROMPT`

### 1B. Pre-filter expansion for newsletters/automated emails
- Add common newsletter/automated sender patterns to the rule-based filter: `*@notifications.*`, `*@updates.*`, `*@marketing.*`, subjects matching `unsubscribe`, `weekly digest`, `monthly report`
- Saves Haiku tokens on obvious skips
- **File:** `worker/run_pipeline.py` — expand blacklist patterns in filter config

### 1C. Feedback loop strengthening
- Lower `FEEDBACK_THRESHOLD` from 2 to 1 for `no_response_needed` corrections (these are high-signal — user explicitly said "don't draft this")
- Add new feedback category: `wrong_tone` (draft was right to exist but tone/formality was off) — feeds into style guide refinement
- **File:** `worker/pipeline/signal_extractor.py` — adjust threshold, add category to `build_feedback_hint()`

### 1D. Enforce rt=none implies draft=false
- The prompt says "If rt=none, draft should almost always be false" but there's no coercion. Add a safety net: if `rt == "none"` and `target != "user"`, force `draft = False`
- Eliminates contradictory classifications
- **File:** `worker/pipeline/signal_extractor.py` — add check in `_coerce_signals()`

---

## Phase 2: Context Awareness
_Better context in = better drafts out._

### 2A. Expand thread context window
- Currently: user_last (1000 chars) + thread_opener (500 chars) — only 2 messages
- Change to: include up to 3 most recent messages (any sender) + thread opener, with descending char limits (800/600/400/400)
- Gives Sonnet more conversational flow to work with
- **File:** `worker/pipeline/drafts.py` — refactor `_build_thread_block()`

### 2B. Attachment awareness in draft prompt
- When email has attachments, add `ATTACHMENTS: [filename1, filename2]` to the user prompt
- The system prompt already handles attachment gaps (Step 4) but the draft prompt doesn't surface attachment names
- **File:** `worker/pipeline/drafts.py` — add attachment block in `_build_draft_prompt()`

### 2C. Sender briefing enrichment
- Currently: contact record fields (type, org, role, significance)
- Add: `last_interaction_summary` — a one-line note about the last email exchange with this sender (from thread stats or recent emails)
- Helps Sonnet understand the relationship arc, not just static profile
- **Files:** `worker/pipeline/drafts.py` (include in SENDER CONTEXT), `worker/run_pipeline.py` (fetch recent interaction)

### 2D. Related thread awareness
- When drafting, check if there are other active threads with the same sender (same conversation_topic prefix or same sender within 7 days)
- Add a brief `RELATED THREADS: [subject1 (3 days ago), subject2 (1 day ago)]` block
- Prevents drafts that ignore parallel conversations
- **Files:** `worker/run_pipeline.py` (query related threads), `worker/pipeline/drafts.py` (include block)

---

## Phase 3: Draft Voice Accuracy
_Sound like the user, not like an AI._

### 3A. Brevity calibration rule
- The style guide identifies ultra-terse mode for internal/routine emails but Sonnet tends toward verbosity
- Add explicit instruction to the system prompt: "If the style guide describes the user as terse or brief for this contact type, target the SHORTEST draft that addresses the need. A 1-3 sentence reply is often correct for internal colleagues."
- **File:** `worker/pipeline/prompts.py` — add to format rules section of `DEFAULT_DRAFT_PROMPT_TEMPLATE`

### 3B. Contact-type tone anchoring
- Currently: "Adjust tone based on recipient: more formal for external legal/lender contacts, conversational for internal colleagues" (generic)
- Replace with: "Match the tone level from the WRITING STYLE GUIDE for this specific contact_type. If the guide describes specific greeting/sign-off patterns for [contact_type], use exactly those patterns."
- Makes the style guide prescriptive rather than advisory
- **File:** `worker/pipeline/prompts.py` — refine format rules in template

### 3C. Common phrase injection
- The style guide captures common phrases ("at long last!", "full steam ahead", "back-of-the-napkin math") but Sonnet rarely uses them
- Add instruction: "When the style guide lists common phrases, actively incorporate 1-2 of them when naturally appropriate. These phrases are the user's verbal fingerprint."
- **File:** `worker/pipeline/prompts.py` — add to format rules

### 3D. Sign-off precision
- Current: "Close with the style guide's sign-off greeting followed by {user_name}"
- Problem: Style guide says user often uses NO sign-off for internal/routine emails
- Fix: "If the style guide indicates no sign-off for this contact type, omit the sign-off entirely. Do not default to 'Best regards' when the user's pattern is to skip it."
- **File:** `worker/pipeline/prompts.py` — refine sign-off instruction

---

## Phase 4: Decision Quality
_Smarter diagnostic vs scaffold vs direct verdicts._

### 4A. Reduce over-scaffolding
- The current prompt's scaffold instructions are detailed but Sonnet still tends to over-scaffold (multi-item USER TO COMPLETE blocks)
- Strengthen the anti-scaffold instruction: "A scaffold draft should NEVER contain more than ONE [USER TO COMPLETE] block. If you find yourself writing multiple scaffold blocks, you've chosen the wrong verdict — switch to diagnostic (ask the ONE question that resolves the most uncertainty) or direct (commit and use [USER TO CONFIRM])."
- **File:** `worker/pipeline/prompts.py` — add to Step 7 verdict section

### 4B. Decisive profile fast-path
- When behavioral profile says "decides" for this situation type, add a shortcut: "If the matching decision disposition rule is 'decides', skip the diagnostic question check entirely. Write a direct reply that commits to a position. Use [USER TO CONFIRM] for any element you're uncertain about."
- Currently the prompt says to "apply a higher bar" but doesn't give a clear fast-path
- **File:** `worker/pipeline/prompts.py` — refine Step 7 behavioral profile check

### 4C. Verdict distribution logging
- Track which verdict (diagnostic/scaffold/direct) each draft uses
- Parse from thinking block: look for "VERDICT: diagnostic|scaffold|direct"
- Store in draft_evaluations table for analysis
- If >40% of drafts are scaffold, the prompt is probably over-scaffolding
- **Files:** `worker/pipeline/drafts.py` (parse verdict from thinking), `worker/run_pipeline.py` (store)

### 4D. Conditional decision quality
- When verdict is diagnostic, the prompt says to give "conditional decision" (if X then Y, if Z then W)
- Strengthen: "The conditional decision must be specific enough that the sender can act on either branch without writing back. Vague conditions like 'if applicable' or 'depending on the situation' are not acceptable."
- **File:** `worker/pipeline/prompts.py` — add to Step 7 diagnostic section

---

## Implementation Order

| Priority | Change | Risk | Effort |
|----------|--------|------|--------|
| 1 | 0A. Draft quality logging | Low | Medium |
| 2 | 0B. Prompt variant tracking | Low | Small |
| 3 | 1D. rt=none safety net | Low | Trivial |
| 4 | 1B. Pre-filter expansion | Low | Small |
| 5 | 3A. Brevity calibration | Low | Small |
| 6 | 3D. Sign-off precision | Low | Small |
| 7 | 3B. Contact-type tone anchoring | Low | Small |
| 8 | 3C. Common phrase injection | Low | Small |
| 9 | 4A. Reduce over-scaffolding | Low | Small |
| 10 | 4B. Decisive profile fast-path | Medium | Small |
| 11 | 2B. Attachment awareness | Low | Trivial |
| 12 | 2A. Expand thread context | Medium | Medium |
| 13 | 1A. CC + high-significance example | Low | Small |
| 14 | 1C. Feedback loop strengthening | Low | Small |
| 15 | 4D. Conditional decision quality | Low | Small |
| 16 | 4C. Verdict distribution logging | Low | Medium |
| 17 | 2C. Sender briefing enrichment | Medium | Medium |
| 18 | 2D. Related thread awareness | Medium | Medium |
| 19 | 0C. Dashboard review panel | Low | Large |

---

## Verification

After each phase deployment:
1. Reset onboarding for test user and rerun
2. Let emails accumulate for 1-2 sync cycles
3. Review generated drafts in devtools panel (once 0C is built, use ratings; until then, manual review)
4. Check Railway logs for classification signals + draft generation metrics
5. Compare thinking blocks against expected verdicts
6. Verify prompt variant tags are populating correctly (after 0B)

---

## Key Files

| File | Phases |
|------|--------|
| `worker/pipeline/prompts.py` | 3A, 3B, 3C, 3D, 4A, 4B, 4D |
| `worker/pipeline/drafts.py` | 0B, 2A, 2B, 2C, 4C |
| `worker/pipeline/signal_extractor.py` | 1A, 1C, 1D |
| `worker/run_pipeline.py` | 0A, 1B, 2C, 2D, 4C |
| `supabase/migrations/` | 0A, 0B (new migration) |
| `web/js/devtools/` | 0C |
