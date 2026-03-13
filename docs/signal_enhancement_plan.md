# Signal Enhancement Plan: Improving Haiku Classification Without Increasing Costs

**Date:** 2026-03-11
**Status:** Proposed
**Scope:** `worker/run_pipeline.py`, `worker/pipeline/enrichment.py`, `worker/pipeline/analyzer.py`, `worker/pipeline/prompts.py`

---

## Executive Summary

Haiku's classification accuracy is bounded not by its reasoning ability but by the quality of the signals it receives. The current pipeline fetches rich context from the database (contacts, threads, topic profiles) but leaves 7 signal fields permanently null/zero, uses shallow regex for intent classification, and presents structural information as prose that Haiku must re-parse rather than as pointed verification questions.

Every enhancement in this plan operates at zero marginal cost — no additional API calls, no extra tokens, no new infrastructure. We are making the existing tokens *smarter*.

---

## Enhancement 1: Backfill Null Signals from Already-Fetched Context

### Vision

The pipeline already pays the database cost to fetch `contacts_map` and `threads_map` in `_fetch_batch_context()`. But `build_signals()` is called *before* that context is available, so 7 fields are hardcoded to null/zero. By the time enrichment runs, we have everything we need — we just never wire it in. This enhancement closes the gap between what we *know* and what we *tell* Haiku.

### Current State

`build_signals()` returns these as permanent fallbacks (`run_pipeline.py:438-446`):

```python
"thread_message_count": None,
"user_replies_in_thread": 0,
"user_active_in_thread": False,
"sender_conditional_response_rate": None,
"sender_emails_last_30d": None,
"thread_velocity": None,
"subsequent_replies_count": 0,
"unique_subsequent_responders": 0,
```

Meanwhile, `contacts_map[sender]` has `response_rate`, `smoothed_rate`, `reply_rate_30d`, `emails_per_month`. And `threads_map[conv_id]` has `total_messages`, `user_messages`, `participation_rate`, `other_responders`, `messages`.

### Implementation

#### Step 1: Create `backfill_signals()` function

**File:** `worker/run_pipeline.py`
**Location:** After `build_signals()` (line ~448)

```python
def backfill_signals(signals, contact, thread_info, thread_messages,
                     user_aliases, current_email_id=None):
    """Backfill null signal fields using fetched context data.

    Called after build_signals() once contacts_map and threads_map
    are available. Mutates signals dict in place.

    Args:
        signals: dict from build_signals().
        contact: dict from contacts_map (or None).
        thread_info: dict from threads_map (or None).
        thread_messages: list of message dicts from thread (or []).
        user_aliases: list[str] of user email addresses (lowercase).
        current_email_id: str or None. The email_id/email_ref of the
            email being classified. If provided, this message is excluded
            from thread_messages before computing subsequent_replies_count
            to avoid counting the inbound email itself as a "reply."
    """
    contact = contact or {}
    thread_info = thread_info or {}
    thread_messages = thread_messages or []

    # Exclude the email being classified from thread messages so it
    # doesn't inflate subsequent_replies_count by counting itself.
    if current_email_id and thread_messages:
        thread_messages = [
            m for m in thread_messages
            if (m.get("id") or m.get("email_ref") or "") != current_email_id
        ]

    # --- Sender signals ---
    signals["sender_conditional_response_rate"] = (
        contact.get("smoothed_rate")
        or contact.get("response_rate")
    )
    signals["sender_emails_last_30d"] = contact.get("emails_per_month")

    # --- Thread signals ---
    if thread_messages:
        signals["thread_message_count"] = len(thread_messages)

        user_msgs = [
            m for m in thread_messages
            if (m.get("sender_email") or "").lower() in user_aliases
        ]
        signals["user_replies_in_thread"] = len(user_msgs)
        signals["user_active_in_thread"] = len(user_msgs) > 0

        # Subsequent replies (messages after the inbound email)
        # thread_messages are already sorted by received_time in threads_map
        non_user = [
            m for m in thread_messages
            if (m.get("sender_email") or "").lower() not in user_aliases
        ]
        if user_msgs:
            last_user_ts = max(
                m.get("received_time") or "" for m in user_msgs
            )
            subsequent = [
                m for m in non_user
                if (m.get("received_time") or "") > last_user_ts
            ]
            signals["subsequent_replies_count"] = len(subsequent)
            signals["unique_subsequent_responders"] = len(set(
                (m.get("sender_email") or "").lower() for m in subsequent
            ))

        # Thread velocity: messages per day
        signals["thread_velocity"] = _compute_thread_velocity(
            thread_messages
        )
    elif thread_info:
        # Use pre-aggregated stats if no raw messages
        signals["thread_message_count"] = thread_info.get("total_messages")
        signals["user_replies_in_thread"] = thread_info.get("user_messages", 0)
        signals["user_active_in_thread"] = (
            thread_info.get("user_messages", 0) > 0
        )
```

#### Step 2: Create `_compute_thread_velocity()` helper

**File:** `worker/run_pipeline.py`

```python
def _compute_thread_velocity(thread_messages):
    """Compute thread velocity label from message timestamps.

    Returns:
        str: "high" (>3/day), "medium" (1-3/day), "low" (<1/day),
             "none" (single message), or "too_early" (thread < 1 hour old).
    """
    if len(thread_messages) < 2:
        return "none"

    timestamps = sorted(
        m.get("received_time") or "" for m in thread_messages
    )
    timestamps = [t for t in timestamps if t]
    if len(timestamps) < 2:
        return "none"

    try:
        first = datetime.fromisoformat(
            str(timestamps[0]).replace("Z", "+00:00")
        )
        last = datetime.fromisoformat(
            str(timestamps[-1]).replace("Z", "+00:00")
        )
        span_hours = (last - first).total_seconds() / 3600

        if span_hours < 1:
            return "too_early"

        msgs_per_day = (len(timestamps) - 1) / max(span_hours / 24, 0.01)

        # Require at least 4 messages before labeling "high" — a
        # 3-message thread over 2 hours computes ~24 msgs/day but is
        # likely just a quick back-and-forth, not a genuinely fast thread.
        if msgs_per_day > 3 and len(timestamps) >= 4:
            return "high"
        elif msgs_per_day >= 1:
            return "medium"
        else:
            return "low"
    except (ValueError, TypeError):
        return "none"
```

#### Step 3: Wire into pipeline

**File:** `worker/run_pipeline.py`, inside `_enrich_batch()` or the enrichment loop

Call `backfill_signals()` after `build_signals()` and before `assemble_enrichment()`:

```python
signals = build_signals(email_data, user_aliases)
backfill_signals(
    signals,
    contact=contacts_map.get(sender_email),
    thread_info=threads_map.get(conv_id),
    thread_messages=thread_messages,
    user_aliases=user_aliases,
    current_email_id=email_data.get("id") or email_data.get("email_ref"),
)
rec = assemble_enrichment(email_data, signals, raw_score, ...)
```

### Impact

- **Thread velocity** lets Haiku know if a conversation is fast-moving (likely needs reply) vs dormant
- **User active in thread** is a strong signal — if the user has never participated, they likely won't start now
- **Sender response rate** tells Haiku the historical baseline before it even reads the email
- **Subsequent replies** indicate whether other people have already responded, reducing the user's obligation

### Testing

```python
def test_backfill_signals_with_contact():
    signals = {"sender_conditional_response_rate": None, ...}
    contact = {"smoothed_rate": 0.72, "emails_per_month": 15}
    backfill_signals(signals, contact, None, [], [])
    assert signals["sender_conditional_response_rate"] == 0.72
    assert signals["sender_emails_last_30d"] == 15

def test_backfill_signals_with_thread():
    # NOTE: The thread message list intentionally does NOT include the
    # inbound email being classified. In production, pass current_email_id
    # to exclude it automatically (see edge case in backfill_signals docs).
    signals = {"thread_message_count": None, "user_replies_in_thread": 0, ...}
    messages = [
        {"sender_email": "user@co.com", "received_time": "2026-03-10T10:00:00Z", "body": "ok"},
        {"sender_email": "bob@co.com", "received_time": "2026-03-10T11:00:00Z", "body": "thanks"},
    ]
    backfill_signals(signals, None, None, messages, ["user@co.com"])
    assert signals["thread_message_count"] == 2
    assert signals["user_replies_in_thread"] == 1
    assert signals["user_active_in_thread"] is True
    assert signals["subsequent_replies_count"] == 1  # Bob's reply after user's message

def test_thread_velocity_high():
    msgs = [
        {"received_time": "2026-03-10T08:00:00Z"},
        {"received_time": "2026-03-10T10:00:00Z"},
        {"received_time": "2026-03-10T12:00:00Z"},
        {"received_time": "2026-03-10T14:00:00Z"},
        {"received_time": "2026-03-10T16:00:00Z"},
    ]
    assert _compute_thread_velocity(msgs) == "high"
```

---

## Enhancement 2: Thread-Aware Feature Check

### Vision

Feature checks are pointed yes/no questions that force Haiku to verify its assumptions. Currently we have 5 generic checks that ignore thread context entirely. Rather than building conditional logic that coaches Haiku on what to conclude, we add one factual thread summary line — just the numbers. Haiku can reason from there.

### Current State

`_build_feature_checks()` (`enrichment.py:360-400`) checks only:
1. User position (TO/CC)
2. Name mention
3. Question mark presence
4. Action language
5. Forward detection

It has no thread context at all.

### Implementation

**File:** `worker/pipeline/enrichment.py`

#### Step 1: Extend signature to accept thread data

```python
def _build_feature_checks(email_data, signals, thread_briefing=None):
```

#### Step 2: Add a single factual thread summary

After the existing 5 checks, append:

```python
    # --- Thread context (single factual line, no coaching) ---
    if thread_briefing and thread_briefing.get("total_messages", 0) > 1:
        total = thread_briefing["total_messages"]
        user_msgs = thread_briefing.get("user_messages", 0)
        participation = thread_briefing.get("participation_rate", 0) or 0
        other_since = signals.get("subsequent_replies_count", 0)

        parts = [f"Thread: {total} messages"]
        parts.append(f"user sent {user_msgs} ({participation:.0%} participation)")
        if other_since:
            parts.append(f"{other_since} replies since user's last message")
        checks.append(", ".join(parts) + ".")
```

#### Step 3: Update caller in `assemble_enrichment()`

```python
    feature_checks = _build_feature_checks(
        email_data, signals,
        thread_briefing=thread_briefing,
    )
```

Note: `thread_briefing` is built *before* `feature_checks` in `assemble_enrichment()`, so this dependency is safe. Sender response rate context is already handled by E4 (base rate explanation) and doesn't need to be duplicated here.

### Impact

- Haiku gets thread facts without opinionated coaching — one line instead of multiple conditional branches
- "Thread: 8 messages, user sent 0 (0% participation)" tells Haiku everything it needs without prescribing a conclusion
- Zero maintenance burden — no thresholds to tune, no branching logic to debug

### Testing

```python
def test_feature_check_thread_summary():
    signals = {"subsequent_replies_count": 2, "user_position": "TO", ...}
    thread_briefing = {
        "total_messages": 5, "user_messages": 2,
        "participation_rate": 0.4,
    }
    checks = _build_feature_checks(
        {"body": "meeting at 3pm"}, signals,
        thread_briefing=thread_briefing,
    )
    assert any("Thread: 5 messages" in c for c in checks)
    assert any("user sent 2" in c for c in checks)
    assert any("2 replies since" in c for c in checks)

def test_feature_check_no_thread():
    signals = {"user_position": "TO", ...}
    checks = _build_feature_checks({"body": "hello"}, signals)
    assert not any("Thread:" in c for c in checks)
```

---

## Enhancement 3: Expand Intent Classification (Minimal)

### Vision

The current intent classifier misses indirect requests — the #1 gap. An email saying "Let me know your thoughts on the attached proposal" has no question mark and no "can you" phrase, so it falls to "unclassified." We fix this by adding indirect request patterns to the existing `direct_request` bucket and adding one new sub-check for FYI emails with embedded requests. No new categories, no cascade rewrite.

### Current State

`build_signals()` (`run_pipeline.py:409-424`) classifies intent as:
- `acknowledgment` — terminal short messages ("thanks", "got it")
- `informational` — FYI language or forwards
- `direct_request` — "can you", "could you", "please", "would you", "need you to"
- `status_update` — "update", "status", "progress"
- `scheduling` — "schedule", "meeting", "call", "calendar"
- `unclassified` — everything else

### Problems

1. **Indirect requests missed:** "Let me know", "your thoughts on", "take a look at", "circle back on"
2. **Multi-intent not detected:** An email can be FYI framing but contain an embedded request

### Implementation

**File:** `worker/run_pipeline.py`

#### Step 1: Add indirect request patterns (line ~396)

```python
    # --- Signal 5b: Indirect request patterns ---
    # These get folded into direct_request — no new category needed.
    # Haiku can distinguish urgency from the email body itself.
    _INDIRECT_REQUEST_RE = re.compile(
        r'\b(?:let me know|let us know'
        r'|your (?:thoughts|feedback|input|take|opinion|view)'
        r'|(?:take|have) a look'
        r'|(?:circle|follow|loop) back'
        r'|(?:get back to|respond to|reply to)\s+(?:me|us)'
        r'|would (?:love|appreciate|like)\s+(?:your|to (?:hear|get|see))'
        r'|(?:weigh in|chime in))\b',
        re.IGNORECASE,
    )
```

#### Step 2: Expand the `direct_request` branch and add `informational_with_request`

Minimal change to the existing cascade — add the indirect regex to the `direct_request` check, and add a sub-check inside the `informational` branch:

```python
    # --- Signal 5: Intent classification ---
    if terminal:
        intent_category = "acknowledgment"
    elif fyi_detected or no_response_detected:
        # Check for embedded request in FYI emails
        if (re.search(r'\b(can you|could you|please|would you|need you to)\b',
                       new_body_lower)
                or _INDIRECT_REQUEST_RE.search(new_body_lower)):
            intent_category = "informational_with_request"
        else:
            intent_category = "informational"
    elif (re.search(r'\b(can you|could you|please|would you|need you to)\b',
                     new_body_lower)
              or _INDIRECT_REQUEST_RE.search(new_body_lower)):
        intent_category = "direct_request"
    elif re.search(r'\b(update|status|progress|where are we)\b', new_body_lower):
        intent_category = "status_update"
    elif re.search(r'\b(schedule|meeting|call|calendar|available)\b', new_body_lower):
        intent_category = "scheduling"
    else:
        intent_category = "unclassified"
```

That's it. Same 6 categories plus one new sub-type (`informational_with_request`). The cascade stays readable without a comment block explaining priority order.

#### Step 3: Add one feature check for the new sub-type

**File:** `worker/pipeline/enrichment.py`

```python
    intent = signals.get("intent_category", "unclassified")
    if intent == "informational_with_request":
        checks.append(
            "Email is mostly FYI but contains an embedded request — "
            "is the request directed at the user specifically?"
        )
```

### Impact

- **Indirect requests** ("let me know your thoughts") now classified as `direct_request` instead of `unclassified`
- **FYI + embedded request** detected as its own sub-type, giving Haiku a useful signal
- **No new categories to maintain** — everything folds into the existing structure
- **~10 lines of new regex**, not a cascade rewrite

### Validation

After implementation, sample 50-100 emails that were previously `unclassified` and verify they're now correctly classified. A wrong label is worse than `unclassified` — see Success Metrics for details.

### Testing

```python
def test_indirect_request_classified():
    signals = build_signals(
        {"body": "Hi Nate, let me know your thoughts on the proposal.",
         "subject": "Project update", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "direct_request"

def test_fyi_with_embedded_request():
    signals = build_signals(
        {"body": "FYI — the vendor sent the final quote. Let me know your thoughts.",
         "subject": "Fw: Vendor quote", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "informational_with_request"

def test_pure_fyi_unchanged():
    signals = build_signals(
        {"body": "FYI — see the attached report.",
         "subject": "Fw: Q1 numbers", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "informational"
```

---

## Enhancement 4: Surface Base Rate Driver in Score Explanation

### Vision

When Haiku sees "Calibrated probability: 12%", it doesn't know *why* — is 12% high for this sender (who normally gets 5% response rate) or low (for a sender who gets 80%)? Without understanding the base rate driver, Haiku can't calibrate its override threshold. This enhancement adds a single sentence explaining *what drove the prior*, turning the score from an opaque number into a reasoning anchor.

### Current State

`_build_score_explanation()` (`enrichment.py:292-325`) outputs:

```
Calibrated probability: 12.0%. Key factors: mentions user name (increases likelihood),
cc only (decreases likelihood), recip mult (decreases likelihood)
```

The factors show *adjustments* but not the *starting point*. The base rate is the most important factor (selected in `score_email()` as `recurring_pattern_rate`, `sender_rate`, or `global_rate`) but it's buried in the factor list alongside multipliers.

### Implementation

**File:** `worker/pipeline/enrichment.py`

#### Step 1: Enhance `_build_score_explanation()` to extract and explain the base rate

```python
def _build_score_explanation(factors, raw_score, calibrated_prob):
    """Render scoring factors as natural language with base rate context."""
    if not factors:
        return f"Raw score {raw_score:.3f} -> calibrated {calibrated_prob:.1%}"

    # Extract base rate driver (always the first factor)
    base_rate_explanation = None
    adjustment_factors = []

    for f in factors:
        if "=" not in f:
            adjustment_factors.append((f, "", 0))
            continue

        name, val_str = f.split("=", 1)
        try:
            val = float(val_str)
        except ValueError:
            adjustment_factors.append((name, val_str, 0))
            continue

        # Identify the base rate factor
        if base_rate_explanation is None and name in (
            "recurring_pattern_rate", "sender_rate", "global_rate"
        ):
            if name == "recurring_pattern_rate":
                base_rate_explanation = (
                    f"Base rate: {val:.0%} (matched a recurring email pattern)"
                )
            elif name == "sender_rate":
                base_rate_explanation = (
                    f"Base rate: {val:.0%} (user's historical response rate "
                    "to this sender)"
                )
            elif name == "global_rate":
                base_rate_explanation = (
                    f"Base rate: {val:.0%} (no sender history — using global "
                    "average)"
                )
            continue

        deviation = abs(val - 1.0) if val > 0.001 else abs(val)
        direction = "increases likelihood" if val > 1.0 else "decreases likelihood"
        adjustment_factors.append(
            (name.replace("_", " "), direction, deviation)
        )

    # Sort adjustments by magnitude, take top 3
    adjustment_factors.sort(key=lambda x: x[2], reverse=True)
    top = adjustment_factors[:3]

    key_factors = ", ".join(
        f"{name} ({direction})" if direction else name
        for name, direction, _ in top
    )

    parts = [f"Calibrated probability: {calibrated_prob:.1%}."]
    if base_rate_explanation:
        parts.append(base_rate_explanation + ".")
    parts.append(f"Key adjustments: {key_factors}")

    return " ".join(parts)
```

### Example Output (before vs after)

**Before:**
```
Calibrated probability: 12.0%. Key factors: sender rate (decreases likelihood),
mentions user name (increases likelihood), cc only (decreases likelihood)
```

**After:**
```
Calibrated probability: 12.0%. Base rate: 45% (user's historical response rate
to this sender). Key adjustments: cc only (decreases likelihood), recip mult
(decreases likelihood), mentions user name (increases likelihood)
```

Now Haiku knows: the user *usually* responds to this sender 45% of the time, but the CC position and large recipient list pulled it down to 12%. This is far more informative for override decisions.

### Impact

- Haiku can distinguish "12% because the sender is unknown" from "12% because it's a high-rate sender who CC'd the user this time"
- Override decisions become calibrated: Haiku should be more willing to override a low-confidence global rate than a sender-specific rate
- The base rate label acts as a **reasoning anchor** — Haiku's first question becomes "does the email content justify deviating from this base?"

### Testing

```python
def test_score_explanation_sender_rate():
    factors = ["sender_rate=0.450", "cc_only=0.800", "recip_mult=0.650"]
    explanation = _build_score_explanation(factors, 0.12, 0.12)
    assert "Base rate: 45%" in explanation
    assert "historical response rate" in explanation
    assert "cc only" in explanation

def test_score_explanation_global_fallback():
    factors = ["global_rate=0.180", "mentions_user_name=1.400"]
    explanation = _build_score_explanation(factors, 0.22, 0.22)
    assert "Base rate: 18%" in explanation
    assert "global average" in explanation

def test_score_explanation_recurring_pattern():
    factors = ["recurring_pattern_rate=0.950", "depth_mult=0.900"]
    explanation = _build_score_explanation(factors, 0.85, 0.85)
    assert "recurring email pattern" in explanation
```

---

## Enhancement 5: Robust Quoted Content Isolation

### Vision

Every signal that reads the email body risks being contaminated by quoted content. A forwarded email might contain "Can you please review this?" in the quoted section from two weeks ago — firing the `direct_request` intent and the question mark check for content that isn't new. The current isolation heuristic splits on "From:" or "-----Original Message" but misses Gmail-style quoting (`>`-prefixed lines), Outlook-style dividers, and inline reply patterns. By improving the boundary detection, every downstream signal becomes more accurate.

### Current State

`build_signals()` (lines 354-358):
```python
new_body = body
for marker in ["From:", "-----Original Message", "________________________________"]:
    idx = body.find(marker)
    if idx > 0:
        new_body = body[:idx]
        break
```

### Problems

1. `"From:"` fires on email addresses in signatures ("From: Sent on behalf of...")
2. Gmail `>` quoting not detected
3. Multiple markers in one email — only the *first* is used, but it might be in a signature
4. Apple Mail uses `On {date}, {name} wrote:` pattern
5. No fallback for emails where the marker is at position 0 (idx > 0 check skips it)

### Implementation

**File:** `worker/run_pipeline.py`

#### Step 1: Create a dedicated `_extract_new_content()` function

```python
_QUOTE_BOUNDARY_PATTERNS = [
    # Outlook-style
    re.compile(r'^-{3,}\s*Original Message\s*-{3,}', re.MULTILINE | re.IGNORECASE),
    re.compile(r'^_{10,}', re.MULTILINE),
    # Gmail/Apple-style "On <date>, <name> wrote:"
    re.compile(r'^On .{10,80} wrote:\s*$', re.MULTILINE),
    # Generic "From: ... Sent: ... To: ..." block (Outlook headers)
    re.compile(r'^From:\s+\S+.*\nSent:\s+', re.MULTILINE),
    # Plain "From:" at start of line (but not in first 5 chars of body).
    # NOTE: The lookbehind (?<=\n) means this won't match "From:" at
    # position 0 of the string (no preceding \n). This is acceptable —
    # position 0 would mean the entire body is quoted, and earliest_pos
    # would be 0, returning an empty string. Not worth a special case.
    re.compile(r'(?<=\n)From:\s+\S+@\S+', re.MULTILINE),
]


def _extract_new_content(body):
    """Extract only the new (non-quoted) content from an email body.

    Tries multiple quote boundary patterns and returns everything
    before the earliest match. Falls back to full body if no
    boundary found.

    Also strips >-prefixed lines (inline quoting).

    Args:
        body: str, raw email body.

    Returns:
        str: new content only.
    """
    if not body:
        return ""

    # Find the earliest quote boundary
    earliest_pos = len(body)
    for pattern in _QUOTE_BOUNDARY_PATTERNS:
        match = pattern.search(body)
        if match and match.start() > 0:
            earliest_pos = min(earliest_pos, match.start())

    new_content = body[:earliest_pos]

    # Strip >-prefixed quoted lines.
    # IMPORTANT: Only strip lines where > is followed by a space or another >
    # (the actual email quoting convention). A bare > could appear in normal
    # content (e.g. ">$500k for the lot") and should not be stripped.
    lines = new_content.split('\n')
    unquoted_lines = [
        line for line in lines
        if not (line.lstrip().startswith('> ')
                or re.match(r'^\s*>[\s>]', line))
    ]

    return '\n'.join(unquoted_lines).strip()
```

#### Step 2: Replace inline isolation in `build_signals()`

Replace lines 354-358:
```python
    new_body = _extract_new_content(body)
```

This single change improves every downstream signal that uses `new_body`: name mention, FYI detection, intent classification.

### Impact

- **False positive reduction** on direct_request, question detection, and name mention for forwarded/replied emails
- **Gmail users** (>50% of email) get correct signal extraction for the first time
- **Inline reply** patterns (`>` quoting) no longer contaminate intent classification

### Testing

```python
def test_extract_outlook_quoted():
    body = "Sounds good, let's proceed.\n\n-----Original Message-----\nFrom: bob\nCan you review this?"
    assert "Can you review" not in _extract_new_content(body)
    assert "let's proceed" in _extract_new_content(body)

def test_extract_gmail_quoted():
    body = "I agree with the approach.\n\nOn Mar 10, 2026, Bob Smith wrote:\n> Can you handle this?"
    new = _extract_new_content(body)
    assert "I agree" in new
    assert "Can you handle" not in new

def test_extract_inline_quotes():
    body = "My responses inline:\n> What about the budget?\nBudget is fine.\n> Timeline?\nEnd of Q2."
    new = _extract_new_content(body)
    assert "Budget is fine" in new
    assert "What about the budget" not in new

def test_extract_no_boundary():
    body = "Simple email with no quoted content."
    assert _extract_new_content(body) == body
```

---

## Implementation Order

| Phase | Enhancement | Effort | Risk | Signal Impact |
|-------|------------|--------|------|---------------|
| 1 | Quoted content isolation (E5) | Small | Low | High — fixes false positives in all other signals |
| 2 | Backfill null signals (E1) | Medium | Low | High — 7 new structured signals |
| 3 | Thread-aware feature check (E2) | Small | Low | Medium — one factual thread summary line |
| 4 | Expand intent classification (E3) | Small | Low | Medium — indirect requests + FYI sub-check |
| 5 | Base rate driver explanation (E4) | Small | Low | Medium — better override calibration |

Phase 1 should come first because every other enhancement reads from the email body and benefits from cleaner content extraction. Phase 2 before Phase 3 because the thread summary in Phase 3 uses backfilled signal values from Phase 2.

---

## Success Metrics

All measurable without additional infrastructure:

1. **"unclassified" intent rate** — track `intent_category == "unclassified"` percentage before/after. Target: drop from ~40% to <15%. **Intermediate validation:** after implementing E3, sample 50-100 emails that were previously unclassified and verify the new classification is actually correct. A drop from 40% to 15% that introduces a 20% misclassification rate in the newly-classified bucket would be worse than staying at 40% unclassified — "unclassified" at least signals honest uncertainty to Haiku, while a wrong label is actively misleading.
2. **Haiku override rate** — percentage of emails where Haiku's `needs_response` disagrees with the scorer's confidence tier. Should become more selective (fewer overrides, higher quality overrides).
3. **Feature check coverage** — average number of feature checks per email. Target: increase from 5 to 6 for threaded emails (one additional thread summary line).
4. **Null signal rate** — percentage of enrichment records with null `sender_conditional_response_rate` or `thread_velocity`. Target: <20% (from current 100%).
5. **False positive audit** — sample emails where intent was `direct_request` and check if the request language was in new vs quoted content.

---

## Files Modified

| File | Changes |
|------|---------|
| `worker/run_pipeline.py` | `backfill_signals()`, `_compute_thread_velocity()`, `_extract_new_content()`, `_INDIRECT_REQUEST_RE` + `informational_with_request` sub-check, wiring in enrichment loop |
| `worker/pipeline/enrichment.py` | `_build_feature_checks()` signature + thread summary line + `informational_with_request` check, `_build_score_explanation()` base rate extraction |
| `worker/pipeline/analyzer.py` | No changes needed — prompt and schema already accommodate richer enrichment |
| `worker/pipeline/prompts.py` | No changes needed — `ENRICHED_ANALYSIS_PROMPT` instructions already tell Haiku to use feature checks and score context |
