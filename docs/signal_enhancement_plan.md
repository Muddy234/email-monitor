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
def backfill_signals(signals, contact, thread_info, thread_messages, user_aliases):
    """Backfill null signal fields using fetched context data.

    Called after build_signals() once contacts_map and threads_map
    are available. Mutates signals dict in place.

    Args:
        signals: dict from build_signals().
        contact: dict from contacts_map (or None).
        thread_info: dict from threads_map (or None).
        thread_messages: list of message dicts from thread (or []).
        user_aliases: list[str] of user email addresses (lowercase).
    """
    contact = contact or {}
    thread_info = thread_info or {}
    thread_messages = thread_messages or []

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

        if msgs_per_day > 3:
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
    signals = {"thread_message_count": None, "user_replies_in_thread": 0, ...}
    messages = [
        {"sender_email": "user@co.com", "received_time": "2026-03-10T10:00:00Z", "body": "ok"},
        {"sender_email": "bob@co.com", "received_time": "2026-03-10T11:00:00Z", "body": "thanks"},
    ]
    backfill_signals(signals, None, None, messages, ["user@co.com"])
    assert signals["thread_message_count"] == 2
    assert signals["user_replies_in_thread"] == 1
    assert signals["user_active_in_thread"] is True
    assert signals["subsequent_replies_count"] == 1

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

## Enhancement 2: Thread-Aware Feature Checks

### Vision

Feature checks are the most powerful lever we have — they're pointed yes/no questions that force Haiku to verify its assumptions against the email content. Currently we have 5 generic checks. By adding thread-aware checks that reference *specific numbers* from the enrichment data, we turn Haiku into a calibrated verifier rather than an independent judge. Instead of "does this need a response?", Haiku answers "the user has replied 3 times in this 5-message thread and the sender has a 72% response rate — does the *content* of this email warrant reply #4?"

### Current State

`_build_feature_checks()` (`enrichment.py:360-400`) checks only:
1. User position (TO/CC)
2. Name mention
3. Question mark presence
4. Action language
5. Forward detection

It ignores: thread participation, sender response rate, thread velocity, thread duration, other replies since user's last message, archetype prediction.

### Implementation

**File:** `worker/pipeline/enrichment.py`

#### Step 1: Extend `_build_feature_checks()` to accept enrichment-stage data

```python
def _build_feature_checks(email_data, signals, contact=None,
                          thread_briefing=None):
```

#### Step 2: Add thread participation checks

After the existing 5 checks, append:

```python
    # --- Thread participation ---
    if thread_briefing and thread_briefing.get("total_messages", 0) > 1:
        user_msgs = thread_briefing.get("user_messages", 0)
        total = thread_briefing["total_messages"]
        participation = thread_briefing.get("participation_rate", 0) or 0

        if user_msgs == 0:
            checks.append(
                f"User has NOT participated in this {total}-message thread — "
                "does this email specifically pull them in, or is it still "
                "not directed at them?"
            )
        elif participation > 0.4:
            checks.append(
                f"User is actively engaged ({user_msgs}/{total} messages, "
                f"{participation:.0%} participation) — is this a continuation "
                "that expects their reply?"
            )

        # Other replies since user's last message
        other_since = thread_briefing.get("other_replies_since", 0)
        if other_since >= 2:
            checks.append(
                f"{other_since} other people have replied since user's last "
                "message — has the user's question/task already been "
                "addressed by others?"
            )
```

#### Step 3: Add sender response rate check

```python
    # --- Sender response rate context ---
    if contact:
        rate = contact.get("smoothed_rate") or contact.get("response_rate")
        if rate is not None:
            if rate > 0.7:
                checks.append(
                    f"User historically responds to {rate:.0%} of emails "
                    "from this sender — is this email an exception "
                    "(FYI, mass send, no action needed)?"
                )
            elif rate < 0.15:
                checks.append(
                    f"User rarely responds to this sender ({rate:.0%} rate) "
                    "— does this email contain an unusually direct or "
                    "urgent request?"
                )
```

#### Step 4: Add thread velocity check

```python
    # --- Thread velocity ---
    velocity = signals.get("thread_velocity")
    if velocity == "high":
        checks.append(
            "This is a fast-moving thread (>3 messages/day) — "
            "is the user expected to jump in, or is the conversation "
            "resolving without them?"
        )
```

#### Step 5: Update caller in `assemble_enrichment()`

```python
    feature_checks = _build_feature_checks(
        email_data, signals,
        contact=contact,
        thread_briefing=thread_briefing,
    )
```

Note: `thread_briefing` is built *before* `feature_checks` in `assemble_enrichment()`, so this dependency is safe.

### Impact

- Haiku stops guessing about thread dynamics and instead verifies specific claims
- "User has NOT participated in this 8-message thread" is far more actionable than "User is CC'd"
- Response rate context prevents Haiku from overriding a statistically strong prior without evidence
- High-velocity thread check catches the common "reply-all storm" pattern

### Testing

```python
def test_feature_check_no_participation():
    signals = {"user_position": "TO", "user_mentioned_by_name": False, ...}
    thread_briefing = {
        "total_messages": 8, "user_messages": 0,
        "participation_rate": 0.0, "other_replies_since": 0,
    }
    checks = _build_feature_checks(
        {"body": "meeting at 3pm"}, signals,
        thread_briefing=thread_briefing,
    )
    assert any("NOT participated" in c for c in checks)

def test_feature_check_high_sender_rate():
    signals = {**base_signals}
    contact = {"smoothed_rate": 0.85}
    checks = _build_feature_checks(
        {"body": "FYI — see attached"}, signals,
        contact=contact,
    )
    assert any("85%" in c for c in checks)
```

---

## Enhancement 3: Improved Intent Classification

### Vision

The current intent classifier uses 5 keyword buckets that miss entire categories of real-world email intent. An email that says "Let me know your thoughts on the attached proposal" has no question mark, no "can you" phrase, and no scheduling keyword — so it falls to "unclassified", forcing Haiku to do all the intent work from scratch. By expanding the regex patterns (still zero-cost), we pre-classify more emails correctly, giving Haiku a reliable starting hypothesis instead of a blank slate.

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
2. **Delegated requests missed:** "Can someone", "who can handle", "anyone available"
3. **Conditional requests missed:** "If you have time", "when you get a chance", "at your convenience"
4. **Approval requests missed:** "Please approve", "sign off on", "green light"
5. **Question-without-question-mark missed:** "I wanted to check if", "wondering whether"
6. **Multi-intent not detected:** An email can be both FYI and contain an embedded request

### Implementation

**File:** `worker/run_pipeline.py`

#### Step 1: Add new pattern groups after the existing ones (line ~396)

```python
    # --- Signal 5b: Expanded intent patterns ---
    indirect_request_patterns = [
        r'\b(?:let me know|let us know)\b',
        r'\byour (?:thoughts|feedback|input|take|opinion|view)\b',
        r'\b(?:take|have) a look\b',
        r'\b(?:circle|follow|loop) back\b',
        r'\b(?:get back to|respond to|reply to)\s+(?:me|us)\b',
        r'\bwould (?:love|appreciate|like)\s+(?:your|to (?:hear|get|see))\b',
        r'\b(?:weigh in|chime in)\b',
    ]
    approval_patterns = [
        r'\b(?:please |kindly )?(?:approve|sign off|green.?light)\b',
        r'\b(?:needs?|requires?|awaiting)\s+(?:your\s+)?(?:approval|sign.?off|authorization)\b',
        r'\b(?:for your|pending your)\s+(?:approval|review|signature)\b',
    ]
    implicit_question_patterns = [
        r'\b(?:wanted to|I\'d like to)\s+(?:check|confirm|verify|ask|see)\b',
        r'\bwondering (?:if|whether|about)\b',
        r'\b(?:curious|interested)\s+(?:if|whether|to (?:know|hear|see))\b',
        r'\bany (?:thoughts|updates|progress|news)\b',
    ]
    delegated_request_patterns = [
        r'\b(?:can|could)\s+(?:someone|anyone|somebody)\b',
        r'\bwho\s+(?:can|could|will|should)\b',
        r'\b(?:anyone|someone)\s+(?:available|able|willing)\b',
    ]
```

#### Step 2: Integrate into the classification cascade

Replace the current cascade (lines 410-424) with a richer version:

```python
    # --- Signal 5: Intent classification (expanded) ---
    if terminal:
        intent_category = "acknowledgment"
    elif fyi_detected or no_response_detected:
        # Check for embedded request in FYI emails
        has_embedded_request = any(
            re.search(p, new_body_lower) for p in indirect_request_patterns
        ) or any(
            re.search(p, new_body_lower) for p in approval_patterns
        )
        intent_category = "informational_with_request" if has_embedded_request else "informational"
    elif subject_type in ("forward", "chain_forward"):
        # Forwards with explicit requests should be classified differently
        has_fwd_request = any(
            re.search(p, new_body_lower) for p in
            indirect_request_patterns + approval_patterns
        ) or re.search(
            r'\b(can you|could you|please|would you|need you to)\b',
            new_body_lower,
        )
        intent_category = "forward_with_request" if has_fwd_request else "informational"
    elif re.search(r'\b(can you|could you|please|would you|need you to)\b', new_body_lower):
        intent_category = "direct_request"
    elif any(re.search(p, new_body_lower) for p in approval_patterns):
        intent_category = "approval_request"
    elif any(re.search(p, new_body_lower) for p in indirect_request_patterns):
        intent_category = "indirect_request"
    elif any(re.search(p, new_body_lower) for p in delegated_request_patterns):
        intent_category = "delegated_request"
    elif any(re.search(p, new_body_lower) for p in implicit_question_patterns):
        intent_category = "implicit_question"
    elif re.search(r'\b(update|status|progress|where are we)\b', new_body_lower):
        intent_category = "status_update"
    elif re.search(r'\b(schedule|meeting|call|calendar|available)\b', new_body_lower):
        intent_category = "scheduling"
    else:
        intent_category = "unclassified"
```

#### Step 3: Update `_FEATURE_CHECK_MAP` for new intent categories

**File:** `worker/pipeline/enrichment.py`

Add entries to `_build_feature_checks()`:

```python
    # Intent-based checks (new categories)
    intent = signals.get("intent_category", "unclassified")
    if intent == "indirect_request":
        checks.append(
            "Indirect request detected (e.g. 'let me know', 'your thoughts') "
            "— is this genuinely asking the user to act, or just a social "
            "courtesy closing?"
        )
    elif intent == "approval_request":
        checks.append(
            "Approval language detected — does the user have authority to "
            "approve, or is this addressed to someone else?"
        )
    elif intent == "delegated_request":
        checks.append(
            "Delegated request ('can someone...') — is the user the most "
            "likely person to handle this, or will someone else pick it up?"
        )
    elif intent == "implicit_question":
        checks.append(
            "Implicit question detected (checking/confirming) — does the "
            "sender expect a reply, or is this rhetorical?"
        )
    elif intent == "informational_with_request":
        checks.append(
            "Email is mostly FYI but contains an embedded request — "
            "is the request directed at the user specifically?"
        )
    elif intent == "forward_with_request":
        checks.append(
            "Forwarded message with an explicit request in the forwarding "
            "note — verify the request is for the user, not just context."
        )
```

### Impact

- **"unclassified" rate drops significantly** — fewer emails arrive at Haiku without a hypothesis
- **Multi-intent detection** (FYI + embedded request) catches the common pattern of "FYI — also, can you review the budget section?"
- **Approval requests** are distinct from general requests — different urgency, different archetype
- **Indirect requests** ("let me know your thoughts") are the #1 missed category in most email classifiers
- **Delegated requests** ("can someone...") are correctly identified as lower-probability for any specific user

### Testing

```python
def test_indirect_request():
    signals = build_signals(
        {"body": "Hi Nate, let me know your thoughts on the proposal.",
         "subject": "Project update", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "indirect_request"

def test_fyi_with_embedded_request():
    signals = build_signals(
        {"body": "FYI — the vendor sent the final quote. Can you approve?",
         "subject": "Fw: Vendor quote", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "informational_with_request"

def test_approval_request():
    signals = build_signals(
        {"body": "The purchase order is ready for your approval.",
         "subject": "PO #4521 — pending approval", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "approval_request"

def test_delegated_not_direct():
    signals = build_signals(
        {"body": "Can someone on the team handle the site inspection Tuesday?",
         "subject": "Site inspection", ...},
        ["nate@co.com"],
    )
    assert signals["intent_category"] == "delegated_request"
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
    # Plain "From:" at start of line (but not in first 5 chars of body)
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

    # Strip >-prefixed quoted lines
    lines = new_content.split('\n')
    unquoted_lines = [
        line for line in lines
        if not line.lstrip().startswith('>')
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
| 3 | Thread-aware feature checks (E2) | Small | Low | High — better verification questions |
| 4 | Improved intent classification (E3) | Medium | Low | Medium — reduces "unclassified" rate |
| 5 | Base rate driver explanation (E4) | Small | Low | Medium — better override calibration |

Phase 1 should come first because every other enhancement reads from the email body and benefits from cleaner content extraction. Phase 2 before Phase 3 because the feature checks in Phase 3 depend on the backfilled signal values from Phase 2.

---

## Success Metrics

All measurable without additional infrastructure:

1. **"unclassified" intent rate** — track `intent_category == "unclassified"` percentage before/after. Target: drop from ~40% to <15%.
2. **Haiku override rate** — percentage of emails where Haiku's `needs_response` disagrees with the scorer's confidence tier. Should become more selective (fewer overrides, higher quality overrides).
3. **Feature check coverage** — average number of feature checks per email. Target: increase from 5 to 7-8 for threaded emails.
4. **Null signal rate** — percentage of enrichment records with null `sender_conditional_response_rate` or `thread_velocity`. Target: <20% (from current 100%).
5. **False positive audit** — sample emails where intent was `direct_request` and check if the request language was in new vs quoted content.

---

## Files Modified

| File | Changes |
|------|---------|
| `worker/run_pipeline.py` | `backfill_signals()`, `_compute_thread_velocity()`, `_extract_new_content()`, expanded intent patterns, wiring in enrichment loop |
| `worker/pipeline/enrichment.py` | `_build_feature_checks()` signature + thread/sender/velocity checks, `_build_score_explanation()` base rate extraction |
| `worker/pipeline/analyzer.py` | No changes needed — prompt and schema already accommodate richer enrichment |
| `worker/pipeline/prompts.py` | No changes needed — `ENRICHED_ANALYSIS_PROMPT` instructions already tell Haiku to use feature checks and score context |
