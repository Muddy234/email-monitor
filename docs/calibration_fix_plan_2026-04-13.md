# Implementation Plan: Calibration Pipeline Fixes

**Date:** 2026-04-13
**Source:** `docs/logs_review_2026-04-08.md`
**Scope:** Six fixes across five files, ordered by priority.

---

## Fix 1 (P0): Contact lookup broken in calibration draft prompts

**Root cause:** `calibration/runner.py:_generate_calibration_drafts()` passes raw `cal_email.incoming_email` as `email_data` to the draft generator. The production pipeline (`run_pipeline.py`) enriches `email_data` with `all_contacts` and `sender_contact` before draft generation — but the calibration path skips this entirely. So `_build_contact_summary()` in `pipeline/drafts.py` always falls through to `"Unknown sender — no contact record."`.

**File:** `worker/calibration/runner.py` — `_generate_calibration_drafts()` (lines 239-314)

**Change:**
- The selector already fetches contacts at line 81 (`db.fetch_contacts_by_emails`), but only uses them for `contact_type` bucketing.
- Pass contacts into `_generate_calibration_drafts()` from `run_calibration()`.
- Before building the batch request, enrich `email_data` with `sender_contact` and/or `all_contacts` by looking up the sender email in the contacts map.

**Specifically:**
1. In `run_calibration()` (~line 80), capture the contacts dict returned by the selector. The selector currently fetches contacts internally — we need to either:
   - (a) Refactor `select_calibration_emails()` to also return the contacts map, **or**
   - (b) Fetch contacts separately in `run_calibration()` using the same sender emails.

   Option (b) is simpler — add a `db.fetch_contacts_by_emails()` call in `run_calibration()` after getting `cal_emails`, using the sender emails from the selected set.

2. Pass the contacts map to `_generate_calibration_drafts()`.

3. In `_generate_calibration_drafts()`, before building each batch request, enrich `email_data`:
   ```python
   sender = (email_data.get("sender_email") or email_data.get("sender") or "").lower()
   contact = contacts_map.get(sender)
   if contact:
       email_data["sender_contact"] = contact
       email_data["all_contacts"] = {sender: contact}
   ```

---

## Fix 2 (P0): Calibration selector picks 100% no-reply emails

**Root cause (confirmed via Supabase queries):** MS Graph assigns different `conversation_id` values to sent vs received emails in the same thread. Of 455 unique sent conversation_ids and 377 received, only **101 are shared**. The remaining 355 sent-only and 276 recv-only IDs represent threads where `_find_user_reply()` silently fails because it only matches on `conversation_id`.

**Data evidence:**
- 1,000 sent emails, all with `conversation_id` populated
- 727 received emails with `conversation_id` (50 null)
- Only 101 conversation_ids overlap between sent and received
- For those 101, the time-ordering logic works correctly
- Subject-based matching confirms real thread overlap exists beyond the 101

**Fix:** Add a subject-based fallback to `_find_user_reply()` when `conversation_id` lookup returns nothing:
1. Keep existing `conversation_id` lookup as primary (it's correct when IDs match).
2. On `None` result, fall back to subject matching:
   - Normalize subjects: strip `Re:`, `Fw:`, `Fwd:` prefixes, lowercase, strip whitespace.
   - Search `sent_by_subject` (a new index built alongside `sent_by_conv`) for sent emails with matching normalized subject.
   - Filter to sent emails within a reasonable time window after the received email (e.g., 48 hours).
   - Return the first match with `len(body.strip()) > 5`.
3. Build `sent_by_subject` index in `_fetch_sent_emails_by_conversation()` (or a new companion function), keyed by normalized subject.

**Additional defensive change:** Add reply ratio logging after candidate pool is built:
```python
reply_count = sum(1 for c in candidates if c.user_reply is not None)
logger.info(f"[CAL-SEL] candidates: {reply_count} with reply, {len(candidates) - reply_count} without")
```

**File:** `worker/calibration/email_selector.py`

---

## Fix 3 (P1): Preference profile leaks Sonnet chain-of-thought

**Root cause:** `SONNET_PREFERENCE_SYNTHESIS_PROMPT` (prompts.py:450-588) asks Sonnet to do Step 1 (per-decision classification) and Step 2 (synthesis), but says "Do not include the per-decision classifications in the output" (line 581). Sonnet sometimes ignores this and includes the full reasoning preamble.

The validation in `synthesis.py:354-366` only checks for the presence of "Investment Orientation:" and "Positional Stance:" in the text — it doesn't strip anything above them.

**File:** `worker/onboarding/synthesis.py` — `synthesize_preferences()` (lines 354-367)

**Change:** Post-process the response to extract only the final profile sections:
```python
# Strip everything before the first trait section header
io_idx = text.find("Investment Orientation:")
ps_idx = text.find("Positional Stance:")
start = min(i for i in (io_idx, ps_idx) if i >= 0)
text = text[start:].strip()
```

Also fix the misleading log at line 362-366: check whether the category value is a real category (not null/omitted), not just whether the header string exists.

---

## Fix 4 (P1): Disable correction rules when cal set lacks ground-truth replies

**Root cause:** When all calibration emails have `has_reply=False`, the scorer rewards "don't draft" and Opus generates rules that suppress all drafting.

**File:** `worker/calibration/runner.py` — in the correction generation block (lines 155-175)

**Change:** Before calling `generate_corrections()`, check how many cal emails have actual replies. If fewer than half have replies, skip correction generation and log a warning:
```python
reply_count = sum(1 for ce in cal_emails if ce.user_reply is not None)
if reply_count < len(cal_emails) // 2:
    logger.warning(
        f"[CAL] iter {iteration}: only {reply_count}/{len(cal_emails)} emails "
        f"have ground-truth replies — skipping correction generation"
    )
else:
    new_rules = generate_corrections(...)
```

---

## Fix 5 (P2): `wc_ratio=999.0` sentinel causes false hard_miss

**Root cause:** In `draft_scorer.py:186-187`, when `actual.word_count == 0` and `draft.word_count > 0`, ratio is set to `999.0`. This triggers hard_miss at line 467.

**File:** `worker/calibration/draft_scorer.py` — `_compute_style_delta()` (line 186-187)

**Change:** Return `not_applicable` semantics when there's no ground-truth reply to compare against:
```python
if actual.word_count > 0:
    ratio = draft.word_count / actual.word_count
else:
    ratio = 0.0  # No ground truth — don't penalize
```

Using `0.0` skips the ratio check at line 466 (`if style.word_count_ratio > 0`), making it neutral rather than punitive.

---

## Fix 6 (P3): Correction rule log truncation

**Root cause:** Line 101 in `correction_generator.py` slices the rule: `rule[:100]`.

**File:** `worker/calibration/correction_generator.py` — line 101

**Change:** Log the full rule text (these are short — typically <300 chars):
```python
logger.debug(f"[CORRECTION] rule {i+1}: {rule}")
```

Or if there's concern about log size, use a larger limit like `rule[:300]`.

---

## Files Modified

| File | Fixes |
|------|-------|
| `worker/calibration/runner.py` | #1 (contact enrichment), #4 (reply-count guard) |
| `worker/calibration/email_selector.py` | #2 (reply finder debugging + logging) |
| `worker/calibration/draft_scorer.py` | #5 (wc_ratio sentinel) |
| `worker/calibration/correction_generator.py` | #6 (log truncation) |
| `worker/onboarding/synthesis.py` | #3 (CoT stripping) |

---

## Verification

1. **Contact lookup:** After fix, run calibration and check logs for `CONTACT SUMMARY:` — should show contact names/types instead of "Unknown sender."
2. **Selector:** Check `[CAL-SEL]` log lines — selected emails should show `has_reply=True` for the majority.
3. **Preference CoT:** Re-run onboarding preference synthesis and verify the stored profile starts with `Investment Orientation:` or `Positional Stance:` — no `## Step` headers.
4. **Correction guard:** With a degenerate cal set, verify `[CAL]` logs show "skipping correction generation" instead of generating suppress-all rules.
5. **wc_ratio:** Verify no `999.0` values appear in `[DRAFT-SCORE]` log lines.
6. **Log truncation:** Verify correction rules are logged in full.
