# Draft Prompt Pipeline Rework

## Summary

Replace the current complex drafting prompt (113-line system prompt with 8-step thinking framework + multi-block user prompt) with a clean, minimal structure:

- **No system prompt** — Sonnet receives only the user prompt
- **Structured user prompt** — PERSONALITY PROFILE → NEVER → EMAIL → THREAD SUMMARY → CONTACT SUMMARY → "Draft a reply."
- **New Haiku thread summary call** — separate batch call, only for draft=true emails with thread history
- **Preference profile converted to plain text** — stored as text like style guide and behavioral profile
- **All-contact lookup** — contact summary includes all participants on the email, not just the sender
- **Thread summaries stored in Supabase** — persisted on the drafts table for auditability and reuse

## Files to Modify

| File | Change |
|------|--------|
| `worker/pipeline/prompts.py` | Remove `DEFAULT_DRAFT_PROMPT_TEMPLATE`. Add `THREAD_SUMMARY_SYSTEM_PROMPT` + `THREAD_SUMMARY_USER_TEMPLATE`. |
| `worker/pipeline/drafts.py` | Rewrite `_build_draft_prompt()`. Add `_build_personality_profile()`, `_build_email_section()`, `_build_contact_summary()`, `_get_never_list()`. Remove `_build_thread_block()`, `_build_preference_block()`. Remove system prompt from `__init__()`. |
| `worker/pipeline/signal_extractor.py` | Add `build_thread_summary_prompt()`, `generate_thread_summary()`, `thread_summary_batch_params()` functions + constants. |
| `worker/run_pipeline.py` | Insert Stage 4c (thread summary Haiku batch) between signal processing and draft generation. Add multi-contact lookup for draft candidates. Wire thread summary storage to drafts table. |
| `worker/onboarding/prompts.py` | Update `SONNET_PREFERENCE_SYNTHESIS_PROMPT` to output plain text instead of JSON. |
| `worker/onboarding/synthesis.py` | Update `synthesize_preferences()` to return plain text string. Remove JSON parsing and category validation. |
| `worker/supabase_client.py` | Update `update_preference_profile()` to store text. Add `thread_summary` column write on draft insert. |
| Supabase migration | Add `thread_summary` column to `drafts` table. Change `preference_profile` column from `jsonb` to `text` (or store text in existing column). |

## Step-by-Step Implementation

### Step 1: Convert preference profile to plain text

**Goal:** Store preference profile as plain text like style guide and behavioral profile, eliminating the need for runtime JSON→text formatting.

**`worker/onboarding/prompts.py`:**
- Update `SONNET_PREFERENCE_SYNTHESIS_PROMPT` (lines 450-596) to instruct Sonnet to output plain text describing the user's investment orientation and positional stance, rather than JSON with category/description/confidence fields.

**`worker/onboarding/synthesis.py`:**
- Update `synthesize_preferences()` (lines 266-367) to:
  - Return `(text_string, usage)` instead of `(dict, usage)`
  - Remove JSON parsing (`json.loads`)
  - Remove category validation against allowed values
  - Keep the minimum 8 decision moments threshold

**`worker/supabase_client.py`:**
- Update `update_preference_profile()` (lines 599-615) to store text directly instead of `json.dumps(dict)`

**Supabase migration:**
- Alter `profiles.preference_profile` from `jsonb` to `text`, or store text string in existing `jsonb` column

**Post-deploy:** Re-run onboarding preference synthesis for existing users to regenerate as text.

### Step 2: Add thread summary Haiku functions (`signal_extractor.py`)

**Goal:** New Haiku call that summarizes thread history for draft candidates.

Add after existing functions (after line ~377):

- `THREAD_SUMMARY_SYSTEM_PROMPT` — instructs Haiku to summarize a thread for a drafting model. Focus: what was discussed/decided, user commitments, unresolved items, key facts. Output: 3-7 sentences plain text.
- `THREAD_SUMMARY_USER_TEMPLATE` — template with `{subject}` and `{thread_content}` placeholders.
- `build_thread_summary_prompt(subject, thread_emails)` — formats thread emails oldest→newest (sender, date, truncated body not to exceed 1000 characters), returns user message string.
- `generate_thread_summary(subject, thread_emails, api_key=None)` — sync Haiku call. Model: haiku, max_tokens: 300, temperature: 0. Returns `(summary_text, usage_dict)`.
- `thread_summary_batch_params(subject, thread_emails, custom_id)` — builds Batches API request dict. Same pattern as `extract_signals_batch_params()`.

### Step 3: Rewrite draft prompt builder (`drafts.py`)

**Goal:** Replace the complex multi-block prompt with a clean, ordered structure. Remove system prompt entirely.

**Update `__init__()`:**
- Remove system prompt template formatting. No system prompt is used.
- `self.system_prompt = None` (or remove the attribute)
- Still store `self.user_name` from config

**Replace `_build_draft_prompt()` with new structure:**

```
PERSONALITY PROFILE:
{style_guide}

{behavioral_profile}

{preference_profile}

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
{thread_summary from action_context, or "No prior thread history."}

CONTACT SUMMARY:
{one-line per contact for all participants}

Draft a reply. If no response is needed, output only: NO_DRAFT_NEEDED: <reason>
```

**New helper methods:**

- `_build_personality_profile(action_context)` — concatenates style_guide + behavioral_profile + preference_profile (all plain text now) with blank line separators. Returns "No personality profile available." if all empty.
- `_get_never_list()` — returns static NEVER guardrails string:
  - Never fabricate information.
  - Never restate the sender's question back to them.
  - Never sign-off or act on behalf of anyone other than the USER.
  - Never answer on behalf of another person or an item that is outside of the USER authority; either produce no draft or acknowledge that the question is for the other person.
  - Never commit to a dollar amount, interest rate, loan term, or financial structure not in the email or thread.
  - Never make a legal commitment outside the confines of the email or thread.
  - Never fabricate a deadline, date, or timeline not in the email or thread.
  - Never address the recipient by the wrong name; use no greeting rather than guessing.
  - Never produce a draft longer than the situation requires.
- `_build_email_section(email_data, action_context)` — builds From/Sent/To/Cc/Subject headers + isolated body. Retains existing `isolate_new_content()` + `truncate_smart()` logic from current `_build_draft_prompt()`.
- `_build_contact_summary(email_data)` — formats ALL contacts on the email (sender + To + Cc) as one line each. Format: `"Name <email> — Contact Type at Org | Role"`. Falls back to `"Unknown sender — no contact record."` if no contacts found.

**Remove:**
- `_build_thread_block()` (lines 150-217) — replaced by Haiku thread summary
- `_build_preference_block()` (lines 219-282) — preference is now plain text, concatenated directly

**Update `generate_draft()` and `build_batch_params()`:**
- Pass `system=None` or omit from API call parameters (no system prompt)
- Remove `cache_system_prompt=True` since there's no system prompt to cache

**No changes to:**
- `_extract_thinking()` — keep as-is; if Sonnet naturally uses `<thinking>` tags, they still get stripped
- `_validate_output()` — keep existing checks (length, error prefix detection, `NO_DRAFT_NEEDED` rejection)

### Step 4: Insert thread summary stage + multi-contact lookup (`run_pipeline.py`)

**Goal:** Generate thread summaries via Haiku batch for draft candidates. Look up contacts for all email participants.

**Stage 4c: Thread Summary Generation**

Insert between signal processing (~line 1155) and Stage 5 draft generation (line 1227). Pattern follows notable summaries batch (lines 1167-1221):

1. Filter `draft_candidates` to those with `thread_emails` in `action_context`
2. For each, call `thread_summary_batch_params(subject, thread_emails, db_id)`
3. Submit batch via `submit_and_wait()`
4. Record token usage: `db.record_token_usage(user_id, "haiku", "thread_summary", usage)`
5. Inject results into `candidate["action_context"]["thread_summary"]`
6. Candidates without threads get `"No prior thread history."`
7. Failed summaries get `"No prior thread history."` (graceful degradation)

**Multi-contact lookup for draft candidates:**

After collecting `draft_candidates` (line ~1137), before Stage 4c:

1. For each draft candidate, collect all unique email addresses from the **current inbound email only** (From + To + Cc fields). Thread participants are not included — the contact summary reflects who is on this specific email, since that's who Sonnet is drafting a reply to.
2. Aggregate all addresses across draft candidates into a single batch-lookup via existing `fetch_contacts_by_emails()` (one DB call for efficiency).
3. Attach the matching contacts to each candidate's `email_data["all_contacts"]` (dict keyed by email address).
4. `_build_contact_summary()` reads this to format one line per participant on the current email.

**Thread summary storage:**

When inserting the draft into Supabase (after QC pass), include the thread summary:
- Add `thread_summary` field to the draft insert payload
- Reads from `candidate["action_context"]["thread_summary"]`

### Step 5: Supabase migration

**New migration file:**

```sql
-- Add thread_summary column to drafts table
ALTER TABLE public.drafts
  ADD COLUMN IF NOT EXISTS thread_summary text DEFAULT NULL;

-- Convert preference_profile from jsonb to text
ALTER TABLE public.profiles
  ALTER COLUMN preference_profile TYPE text USING preference_profile::text;
```

## Out of Scope

- **Edge function** (`supabase/functions/generate-draft/index.ts`) — left as-is
- **QC pipeline** (`quality_check.py`) — left as-is
- **Pre-synthesized personality profile** — future work; for now, layer the 3 existing text profiles
- **Signal extraction prompt** — unchanged

## Verification

1. Run the worker pipeline against a batch with mixed emails (threaded + standalone, internal + external)
2. Check Haiku thread summaries are concise and accurate (inspect logs)
3. Compare Sonnet draft quality before/after by reviewing generated drafts
4. Verify QC still passes on new-format drafts
5. Verify fallback when thread summary batch fails (drafts should still generate)
6. Check token usage recording for new "haiku/thread_summary" category
7. Verify multi-contact lookup populates all participants
8. Verify thread summaries are stored on drafts table
9. Verify preference profile stores and reads as plain text after re-onboarding
