# Sign-off & Greeting Strip Plan

**Status:** Planning. No code changes applied.

**Goal:** Eliminate the recurring "wrong name in sign-off" bug by removing greetings and sign-offs from drafts at the QC boundary. The user (and/or OWA's signature config) handles them at review/send time.

**Strategy:** Convert the existing QC validation functions for sign-off and greeting into unconditional strip functions, with tightened regex/structural guards to prevent over-stripping. The drafter continues to produce natural emails as before; QC discards the parts that cause identity-confusion bugs before the draft is stored.

---

## Background

### Current bug
The drafter LLM occasionally signs replies with the wrong name (e.g., "Best, Connor" instead of "Best, Nate") when other participants' names appear prominently in the prompt context (thread summary, contact summary, body of the email being replied to). Greetings can exhibit the analogous bug ("Hi Connor," when the reply is to Tyler).

### Why this approach
At the product's current stage, the simplest possible fix is the right one:

- **Bug becomes structurally impossible** for the targeted symptom. No greeting/sign-off in stored draft → no name to be wrong.
- **One file changed.** `worker/pipeline/quality_check.py` only.
- **Zero prompt changes.** Drafter continues to do its natural thing.
- **Zero schema changes.** No tool use, no compiler, no new modules.
- **Zero downstream changes.** `draft_body` remains an opaque string at storage boundary; extension, web dashboard, RPCs, RLS, edge functions all unaffected.
- **Reversible** with a single revert.
- **Future-friendly.** If product later wants AI-generated greetings/sign-offs, the structured tool-use approach designed earlier remains a viable path forward.

### What this approach does NOT fix
This plan targets a **symptom**, not the underlying mechanism. The drafter sees other people's names in prompt context and pulls them into the sign-off because identity is being decided by an LLM reading a flood of names. Stripping the sign-off makes the loud failure mode go away. It does **not** address whether other parts of the draft body are also drifting toward those other identities — stance, vocabulary, framing.

A manual quality review window (see Success Criteria) is required to detect this. The QC metrics alone will read green by construction.

### Trade-offs accepted
- Drafts arrive without greeting or sign-off. User adds these when reviewing, OR OWA signature config inserts the sign-off on send.
- Drafter still generates greetings/sign-offs that get discarded — minor token waste.
- The retry-on-bad-sign-off path becomes dead code (no remaining QC check sets `needs_retry`).

---

## Architecture

### Current flow

```
Drafter (free text) → QC (validate signoff/greeting, retry if wrong) → insert_draft → realtime → OWA
```

### New flow

```
Drafter (free text) → QC (strip signoff + greeting unconditionally, with guards) → insert_draft → realtime → OWA
```

The only change is the behavior of two QC checks plus a small comment in `run_pipeline.py`. Everything else is unchanged.

---

## Pre-deploy verification: OWA signature config

Before deploying, verify the user's Outlook signature setup:

- **If OWA has an auto-signature configured for replies**, the sign-off (`Best,\nNate`) gets appended on send. Drafts will look "incomplete" in the review pane (body only) but go out complete. This is the good outcome — review friction is minimal.
- **If no auto-signature**, every sent reply requires manually typing `Best, Nate` before send. Recommended action: configure a default reply signature in OWA (one-time, ~60 seconds) before this ships.

For greetings, there is no OWA equivalent — they are always per-recipient and must be typed at review time. Friction here is real and proportional to draft volume.

This step is a **deploy gate**, not a deferred follow-up.

---

## Component changes

### `worker/pipeline/quality_check.py`

#### `_check_signoff` → `_strip_signoff`

- Removed: name-correctness validation, missing-signoff detection, double-signoff dedup, retry triggering.
- New behavior: find candidate sign-off matches with `_SIGNOFF_RE`, then **only strip if the match is structurally trailing** (see "Trailing-signoff guard" below).
- Walks back to the start of the line containing the sign-off phrase and truncates everything from there to end of draft.
- Returns `auto_fixed=["stripped_signoff:<N>"]` where `<N>` is the number of characters removed (telemetry — see "Strip-length logging").
- If a match exists but the trailing-signoff guard rejects it, returns `auto_fixed=["signoff_strip_skipped_midbody"]` and leaves the draft untouched.
- Always runs (no longer gated by `signoff_expected`).
- Never sets `needs_retry`.

**Trailing-signoff guard:**
```python
def _looks_like_trailing_signoff(draft: str, match: re.Match) -> bool:
    """True if the sign-off match is structurally at the end of the draft.

    Accepts: phrase line followed by 0-4 short lines (≤5 words each)
    representing a name/title/contact signature block.
    Rejects: phrase appearing mid-body with substantive content following.
    """
    tail = draft[match.end():].strip()
    if not tail:
        return True
    tail_lines = [l.strip() for l in tail.split("\n") if l.strip()]
    if len(tail_lines) > 4:
        return False
    return all(len(l.split()) <= 5 for l in tail_lines)
```

**Defensive cap (belt-and-suspenders):** if the strip would remove more than 200 characters, skip and flag `signoff_strip_skipped_too_long`. Provides a backstop if the structural guard ever misjudges. The constant is conservative — typical sign-off blocks are 20-80 chars.

#### `_check_greeting` → `_strip_greeting`

- Removed: missing-greeting detection, wrong-recipient detection, retry triggering.
- New behavior: find the first non-empty line; **only strip if it matches a tightened greeting regex** (see below). Drops any blank lines that follow.
- Returns `auto_fixed=["stripped_greeting:<N>"]` where `<N>` is the character count removed.
- Always runs (no longer gated by `greetings_expected`).
- Never sets `needs_retry`.

**Tightened greeting regex** (replaces the loose detection regex used for validation):
```python
_GREETING_STRIP_RE = re.compile(
    r'^\s*(?:Hi|Hello|Hey|Dear|Good\s+(?:morning|afternoon|evening))'
    r'(?:[ \t]+[\w\'-]+){0,3}\s*[,!]\s*$',
    re.IGNORECASE,
)
```

Key differences from existing `_GREETING_RE`:
- Must end with `,` or `!` (greetings are structurally punctuated)
- Allows 0-3 trailing words (covers "Hi Tyler,", "Hi all,", "Hi everyone,", "Dear Mr. Smith,", "Hi Tyler and Rebecca,")
- Anchored to start AND end of (single) line — prevents matching `"Hi — quick thought"`
- Original `_GREETING_RE` is left in place for now (unused after this change but out of scope to remove)

#### `check_draft_quality`
- Updated check list to call `_strip_signoff` and `_strip_greeting` instead of the old check functions.
- All other checks unchanged.

#### `build_revision_notes`
- Simplified to return `""`. The sign-off and greeting issue codes it formerly handled no longer exist; remaining issue codes (`unclosed_bracket`, `bare_placeholder`, `draft_too_short`, `draft_too_long`) are flag-only.

### `worker/run_pipeline.py`

Add a comment above the QC retry branch (~line 1435):

```python
# NOTE [as of YYYY-MM-DD]: With sign-off and greeting validation removed
# from QC (see worker/pipeline/quality_check.py), no remaining QC check
# sets needs_retry=True. This branch is currently unreachable. If a future
# QC check wants to trigger retries, verify build_revision_notes() emits
# meaningful guidance for the new issue codes — it currently returns "".
```

No behavioral change. Documentation only. Prevents future debugging when someone adds a `needs_retry`-emitting check and is surprised retries don't seem to work as expected.

### What deliberately does NOT change

- `QCConfig` fields (`signoff_expected`, `greetings_expected`, `multi_recipient`, `recipient_name`, `target_word_range`) — left in place. The strip functions don't read them, but `parse_qc_config` still populates them. Dead fields are out of scope to remove now.
- `parse_qc_config` — still parses style guide as before.
- `_check_leaked_artifacts`, `_check_tag_hygiene`, `_check_length` — untouched.
- Existing `_SIGNOFF_RE` and `_GREETING_RE` patterns — left in place. New `_GREETING_STRIP_RE` added alongside.
- `worker/pipeline/drafts.py` (drafter) — untouched.
- `extension/`, `web/`, edge functions, DB schema — untouched.

---

## Telemetry

### New issue codes

| Code | Meaning |
|------|---------|
| `stripped_signoff:<N>` | Sign-off block was removed; `<N>` chars stripped |
| `signoff_strip_skipped_midbody` | Sign-off phrase matched but structural guard rejected — left in place |
| `signoff_strip_skipped_too_long` | Strip would have removed >200 chars; defensive backstop fired |
| `stripped_greeting:<N>` | Greeting line was removed; `<N>` chars stripped |

### Removed issue codes
- `missing_signoff`, `truncated_signoff_name`, `wrong_signoff_name`, `missing_name_after_signoff`, `removed_duplicate_signoff` — no longer raised
- `missing_greeting`, `greeting_addresses_user` — no longer raised

### Monitoring queries

Track in `drafts.quality_issues`:

1. **Strip fire rate.** Expect `stripped_signoff:*` on ~95%+ of new drafts. If lower, drafter output isn't matching the regex — phrase list may need expansion.
2. **Over-strip candidates.** Query: `quality_issues LIKE 'stripped_signoff:%'` AND parsed N > 100. Manually review these — they may indicate the structural guard let through a mid-body match. Same for `stripped_greeting:*` with N > 60.
3. **Skip flags.** `signoff_strip_skipped_midbody` and `signoff_strip_skipped_too_long` should be rare. If they fire frequently, investigate.
4. **Old bug class baseline.** Pre-change rows: count `wrong_signoff_name`, `truncated_signoff_name`, etc. New rows: must be flat zero.

---

## Risks and known limitations

### Over-stripping (mitigated, not eliminated)
The combination of trailing-signoff guard + 200-char cap + tightened greeting regex addresses the bulk of the over-strip risk. Residual scenarios:
- Drafter writes a very short legitimate body that ends with a transition phrase the regex matches. Should be rare given the structural guard.
- Drafter quotes a previous email and the quote ends with a sign-off followed by a short attribution. Tail might look like a signature block.

**Mitigation:** the per-strip char count in `auto_fixed` enables post-hoc detection. Manual review of outliers (>100 chars) catches edge cases.

### Strip robustness
- `_SIGNOFF_RE` was originally designed for *detection*, not *deletion*. Real-world drafter output may use phrase variants the regex doesn't match (`"Talk soon,"`, `"Looking forward,"`, `"Take care,"`, foreign-language sign-offs).
- **Mitigation:** monitor `stripped_signoff` fire rate; if <95%, expand the phrase list.

### Body coherence
- Drafts may end abruptly without a closing phrase. Expected to be rare since most bodies end with a complete thought before transitioning to the sign-off.

### User UX
- OWA drafts appear without greeting/sign-off. If OWA signature config is set up, sign-off is added on send. Greetings always require manual typing.
- This is a positioning shift from "AI writes complete emails" to "AI drafts the substance, you add polish."

### Retry mechanism becomes dead code
- After this change, no QC check sets `needs_retry`, so `qc.passed` is always `True` and the retry branch in `run_pipeline.py` never executes.
- Code is preserved structurally for future use. **Mitigation:** comment added to flag this for future contributors (see `run_pipeline.py` change above).

### Voice bleed (the unaddressed deeper issue)
- Stripping the sign-off does not address whether the body of the draft is also being influenced by other named identities in prompt context (stance bleed, vocabulary bleed, framing bleed).
- QC metrics will not detect this — they are structural, not semantic.
- **Mitigation:** mandatory manual review window (see Success Criteria).

---

## Success criteria

This change ships only when **all** of the following hold:

### Quantitative (automated)
- [ ] Zero `wrong_signoff_name`, `truncated_signoff_name`, `missing_name_after_signoff` issue codes in new draft rows.
- [ ] `stripped_signoff:*` appears on >95% of new draft rows. (If lower, regex needs expansion before declaring success.)
- [ ] `stripped_greeting:*` appears on a majority of new draft rows.
- [ ] `signoff_strip_skipped_midbody` and `signoff_strip_skipped_too_long` fire on <2% of rows.
- [ ] No regressions in extension realtime delivery, OWA draft push, or web dashboard rendering.

### Qualitative (manual, mandatory)
- [ ] **Two-week voice-bleed review window after deploy.** Sample-read 3-5 drafts per day with the question "would I have written this?" — not "is the sign-off right?". Look for:
  - **Stance bleed:** drafter agrees with parent email's position when user would push back
  - **Vocabulary bleed:** phrases from parent appearing in draft user wouldn't use
  - **Framing bleed:** draft accepts parent's framing of a question instead of reframing
- [ ] At end of two weeks, document findings. If voice bleed is observed, escalate — the structured tool-use approach (in git history) becomes the next conversation.

A green dashboard with no manual read-through does NOT count as success. Both gates required.

---

## Implementation steps

### Phase 1: Code change
1. Add `_GREETING_STRIP_RE` constant.
2. Add `_looks_like_trailing_signoff()` helper.
3. Replace `_check_signoff` with `_strip_signoff` (with trailing guard + 200-char cap + char-count flag).
4. Replace `_check_greeting` with `_strip_greeting` (with tightened regex + char-count flag).
5. Update check list in `check_draft_quality`.
6. Simplify `build_revision_notes` to return `""`.
7. Add explanatory comment to retry branch in `run_pipeline.py`.

### Phase 2: Local verification
8. Spot-check: pick 3-5 recent drafts (from logs or DB) including at least one that had `wrong_signoff_name` flagged. Run them through `check_draft_quality` locally. Confirm:
   - Sign-off and greeting are stripped cleanly.
   - No body content is removed unexpectedly.
   - `auto_fixed` flags include char counts.

### Phase 3: Pre-deploy
9. Verify OWA signature config (see Pre-deploy verification section above). Configure a reply signature if not already set.

### Phase 4: Ship
10. Deploy to production.
11. Monitor `quality_issues` distribution for 3-7 days using the queries in the Telemetry section.
12. Confirm quantitative success criteria.

### Phase 5: Voice-bleed review (mandatory)
13. Two-week manual review window (3-5 drafts/day).
14. Document findings.

### Phase 6 (conditional): Iterate
15. If users complain that drafts feel incomplete despite OWA signature: evaluate client-side greeting prepend in extension.
16. If voice bleed is observed in Phase 5: revisit structured tool-use approach (archived in git history).
17. If regex misses observed phrase variants: expand phrase list.

---

## Rollback plan

Single-file revert:
```
git revert <commit-sha>
```

Restores the validation-and-retry behavior. No data migration needed. Existing drafts in DB are unaffected (already-stripped drafts stay stripped; new drafts will have greetings/sign-offs again).

**Important:** rollback also restores the `wrong_signoff_name` bug class to production. Acceptable in an emergency-revert scenario, but worth knowing.

---

## Out of scope

- Removing unused `QCConfig` fields.
- Removing dead retry branch in `run_pipeline.py`.
- Pruning `parse_qc_config` style-guide parsing for now-unused fields.
- Updating `@pipeline` annotations.
- Cleaning up the prompt instructions that ask the drafter for greeting/sign-off.
- Removing the original `_SIGNOFF_RE` and `_GREETING_RE` patterns.

These are reasonable follow-up cleanups, deferred to keep the diff small and the revert clean.
