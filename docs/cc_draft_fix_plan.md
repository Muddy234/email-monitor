# Fix: CC emails generating unnecessary drafts

## Problem
Haiku receives the full 2500-token thread body for signal extraction. Older messages with financial/OCIP terms overwhelm the "focus on newest message only" instruction, causing it to flag CC acknowledgments as needing drafts. Sonnet then has no option to reject — it's forced to write a reply.

## Change B: Isolate newest message for Haiku

### `worker/pipeline/pre_process.py` — `pre_process_email()` (line 348-360)
- Wire up `isolate_new_content()` when `prior_bodies` is provided
- `isolate_new_content` already exists (line 114), is battle-tested (used by Sonnet's draft path in `drafts.py:48`), handles forwards, empty bodies, and no-match fallback
- Truncation still applies after isolation as a ceiling
- Remove "Unused" note from `prior_bodies` docstring

### `worker/run_pipeline.py` — sync fallback path (line 999)
- Compute `prior_bodies` from `thread_emails_map` (same as batch path at line 914-916) and pass to `pre_process_email`
- `thread_emails_map` and `ctx["conv_id"]` are both in scope

## Change C: Sonnet rejection valve

### `worker/pipeline/prompts.py` — `DEFAULT_DRAFT_PROMPT_TEMPLATE`
- Add rejection instruction after Step 8, before "Then generate an email reply" (between current lines 68-69)
- Sonnet can output `NO_DRAFT_NEEDED: <reason>` instead of a draft after completing its thinking analysis
- Instruction includes when to reject (CC awareness-only, terminal ack directed at someone else, purely informational, sender confirmed they'll handle it) and when NOT to reject (reason says user needs to respond, genuine question to user, user is action target)
- Add explicit "when in doubt, draft" bias: "If you are uncertain whether a draft is needed, generate the draft. A user can discard an unnecessary draft more easily than they can notice a missing one."
- Cost note: rejected drafts still consume full thinking tokens. Acceptable for correctness but log rejection counts — if CC ack volume is high, a future lightweight pre-screen could reduce cost

### `worker/pipeline/drafts.py` — `_validate_output()` (line 364-408)
- Add rejection marker detection after `<thinking>` stripping (line 387) but before the `len < 20` check (line 389)
- If `cleaned` starts with `NO_DRAFT_NEEDED:`, extract reason, log at INFO as "Draft REJECTED by model", return None
- None feeds into the existing drop path — no changes needed downstream

## Verification
1. Reset the Loraloma email to unprocessed, delete existing draft
2. Run pipeline, confirm Haiku receives isolated newest message (check log for body length)
3. If Haiku still says `draft=true`, confirm Sonnet rejects with `NO_DRAFT_NEEDED` and no draft is stored
4. Test a real TO email that needs a draft still generates correctly (no regression)
5. Test boundary case: reply-all where user is TO but newest message addresses someone else by name (e.g., "Sarah, can you handle the vendor follow-up? Nate, just keeping you in the loop.") — verify Haiku/Sonnet handle the split-attention correctly
