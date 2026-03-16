# Chain-of-Thought Reasoning for Email Draft Generation

## Status: Implemented

## Problem
The AI drafting system sometimes produces logically flawed responses (e.g., asking for information already provided in the email). The model jumped straight to composing without first understanding the situation.

## Solution
Added a `<thinking>` step to the drafting prompt so the model reasons through the email context before writing. The thinking block is stripped before saving the draft.

## Changes Made

### 1. `worker/pipeline/prompts.py`
Updated `DEFAULT_DRAFT_PROMPT_TEMPLATE` with a 5-point thinking framework:
1. **Situation** — What is happening? What is the broader context?
2. **Sender's intent** — What does the sender actually need from me?
3. **Key information** — What facts/details are already established?
4. **Tone** — What is the conversational register? Formal, casual, urgent?
5. **Useful response** — What reply would be most helpful and move things forward?

Also added an explicit guardrail: "Never asks for information the sender already provided or that is already available from the email context."

### 2. `worker/pipeline/drafts.py`
Updated `_validate_output()` to strip `<thinking>` tags before validation and storage:
- Non-greedy `re.sub()` to avoid eating the draft body on malformed tags
- Fallback: if a lone `<thinking>` tag remains (unclosed), logs a warning and strips the prefix
- Comment explains the strip prevents thinking tags from surfacing in the extension UI

## Files NOT Changed
- `run_pipeline.py` — Both pipelines already pass through `_validate_output()` before saving
- `api_client.py` — Model params unchanged (4096 max tokens, 0.3 temperature)

## Verification Checklist
- [ ] Happy path: `<thinking>` block appears in raw output, stripped before DB insert
- [ ] Regression: Existing draft quality maintained on normal emails
- [ ] Negative test: Ambiguous email produces clarifying response, not fabricated answer
