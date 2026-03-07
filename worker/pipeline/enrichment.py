"""Enrichment assembly — builds structured context records for Haiku classification.

All summaries are template-generated (f-strings, no LLM calls). Each enrichment
record contains everything Haiku needs to make a classification decision:
sender briefing, thread briefing, score explanation, feature checks, anomaly
flags, archetype prediction, time pressure, and selected messages.
"""

import re
from datetime import datetime, timezone


def assemble_enrichment(email_data, signals, raw_score, calibrated_prob,
                        confidence_tier, factors, contact, thread_messages,
                        user_aliases, profile):
    """Build a complete enrichment record for a single email.

    Args:
        email_data: dict from supabase_row_to_email_data().
        signals: dict from build_signals().
        raw_score, calibrated_prob, confidence_tier, factors: from score_email().
        contact: dict from contacts table (or None).
        thread_messages: list of message dicts from conversations.messages jsonb (or []).
        user_aliases: list[str] of user email addresses (lowercase).
        profile: user profile dict.

    Returns:
        dict: Enrichment record.
    """
    contact = contact or {}
    thread_messages = thread_messages or []

    sender_briefing = _build_sender_briefing(email_data, contact, signals)
    thread_briefing = _build_thread_briefing(email_data, thread_messages, user_aliases)
    score_explanation = _build_score_explanation(factors, raw_score, calibrated_prob)
    feature_checks = _build_feature_checks(email_data, signals)
    anomaly_flags = _build_anomaly_flags(email_data, signals, contact)
    archetype = _predict_archetype(contact)
    time_pressure = _detect_time_pressure(email_data)
    messages = _select_messages(email_data, thread_messages, user_aliases)

    return {
        "email_id": email_data.get("_db_id") or email_data.get("email_ref", ""),
        "raw_score": raw_score,
        "calibrated_probability": calibrated_prob,
        "confidence_tier": confidence_tier,
        "sender_briefing": sender_briefing,
        "thread_briefing": thread_briefing,
        "score_explanation": score_explanation,
        "feature_checks": feature_checks,
        "anomaly_flags": anomaly_flags,
        "archetype_prediction": archetype,
        "time_pressure": time_pressure,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Sender briefing
# ---------------------------------------------------------------------------

def _build_sender_briefing(email_data, contact, signals):
    """Build sender context from contact profile + email metadata."""
    sender_email = (email_data.get("sender_email") or email_data.get("sender") or "").lower()
    sender_name = email_data.get("sender_name") or sender_email.split("@")[0]
    domain = sender_email.split("@")[1] if "@" in sender_email else "unknown"

    contact_type = contact.get("contact_type", "unknown")
    is_internal = contact_type == "internal" or domain == "arete-collective.com"
    role = contact.get("inferred_role", "unknown")
    org = contact.get("inferred_organization", domain.split(".")[0].title())
    epm = contact.get("emails_per_month", 0) or 0
    rate = contact.get("response_rate")
    latency = contact.get("avg_response_time_hours")

    # Relationship direction
    user_initiates = contact.get("user_initiates_pct")
    if user_initiates is not None:
        if user_initiates > 0.6:
            direction = "user initiates most"
        elif user_initiates < 0.4:
            direction = "sender initiates most"
        else:
            direction = "balanced"
    else:
        direction = "unknown"

    # Frequency label
    if epm >= 20:
        freq_label = "very frequent"
    elif epm >= 5:
        freq_label = "regular"
    elif epm >= 1:
        freq_label = "occasional"
    else:
        freq_label = "rare"

    # Response length label
    avg_body = contact.get("avg_response_body_length") or contact.get("user_avg_body_length")
    if avg_body is not None:
        if avg_body > 500:
            length_label = "detailed"
        elif avg_body > 150:
            length_label = "moderate-length"
        else:
            length_label = "brief"
    else:
        length_label = None

    # Latency label
    if latency is not None:
        if latency < 1:
            latency_label = "under an hour"
        elif latency < 24:
            latency_label = f"{latency:.0f} hours"
        else:
            latency_label = f"{latency / 24:.0f} days"
    else:
        latency_label = None

    # Summary template
    if contact.get("relationship_summary"):
        summary = contact["relationship_summary"]
    elif rate is not None:
        type_str = "internal" if is_internal else "external"
        summary = f"{sender_name} is a {freq_label} {type_str} contact. "
        summary += f"User responds to {rate:.0%} of their emails"
        if length_label and latency_label:
            summary += f", typically {length_label} replies within {latency_label}"
        elif latency_label:
            summary += f", typically within {latency_label}"
        summary += "."
    else:
        summary = f"{sender_name} — New sender, no prior history"

    return {
        "name": sender_name,
        "email": sender_email,
        "domain": domain,
        "domain_category": contact_type,
        "is_internal": is_internal,
        "reply_rate": rate,
        "emails_per_month": epm,
        "relationship_direction": direction,
        "avg_response_latency_hrs": latency,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Thread briefing
# ---------------------------------------------------------------------------

def _build_thread_briefing(email_data, thread_messages, user_aliases):
    """Build thread context from conversation messages."""
    subject = email_data.get("subject") or "(no subject)"
    received_time = email_data.get("received_time")

    if not thread_messages:
        return {
            "subject": subject,
            "total_messages": 1,
            "user_messages": 0,
            "participation_rate": None,
            "user_initiated": False,
            "user_avg_body_length": 0,
            "user_engagement": "none",
            "thread_duration_days": 0,
            "messages_today": 1,
            "other_replies_since": 0,
            "other_replies_summary": None,
            "summary": "New conversation",
        }

    total = len(thread_messages)
    user_msgs = [
        m for m in thread_messages
        if (m.get("sender_email") or "").lower() in user_aliases
    ]
    user_count = len(user_msgs)
    participation = user_count / total if total > 0 else 0

    # User initiated?
    sorted_msgs = sorted(thread_messages, key=lambda m: m.get("received_time") or "")
    first_sender = (sorted_msgs[0].get("sender_email") or "").lower() if sorted_msgs else ""
    user_initiated = first_sender in user_aliases

    # User engagement
    avg_body_len = 0
    if user_msgs:
        lengths = [len(m.get("body") or "") for m in user_msgs]
        avg_body_len = sum(lengths) / len(lengths)
    if avg_body_len < 50:
        engagement = "brief"
    elif avg_body_len > 200:
        engagement = "substantive"
    else:
        engagement = "mixed"

    # Thread duration
    duration_days = 0
    if len(sorted_msgs) >= 2:
        try:
            first_ts = _parse_ts(sorted_msgs[0].get("received_time"))
            last_ts = _parse_ts(sorted_msgs[-1].get("received_time"))
            if first_ts and last_ts:
                duration_days = max(0, (last_ts - first_ts).days)
        except Exception:
            pass

    # Messages today
    messages_today = 0
    if received_time:
        try:
            inbound_dt = _parse_ts(received_time)
            if inbound_dt:
                for m in thread_messages:
                    mt = _parse_ts(m.get("received_time"))
                    if mt and mt.date() == inbound_dt.date():
                        messages_today += 1
        except Exception:
            pass
    messages_today = max(messages_today, 1)

    # Other replies since user's last message
    other_since = 0
    other_senders = set()
    if user_msgs:
        last_user_ts = max(
            (_parse_ts(m.get("received_time")) for m in user_msgs),
            default=None,
        )
        if last_user_ts:
            for m in thread_messages:
                sender = (m.get("sender_email") or "").lower()
                if sender in user_aliases:
                    continue
                mt = _parse_ts(m.get("received_time"))
                if mt and mt > last_user_ts:
                    other_since += 1
                    other_senders.add(sender)

    other_summary = None
    if other_since > 0:
        senders_str = ", ".join(list(other_senders)[:3])
        other_summary = f"{other_since} replies from {senders_str}"

    # Thread age label
    if duration_days == 0:
        age_label = "new (started today)"
    elif duration_days <= 1:
        age_label = "recent (1 day old)"
    elif duration_days <= 7:
        age_label = f"active ({duration_days} days old)"
    else:
        age_label = f"long-running ({duration_days} days old)"

    # Other replies description
    if other_since > 0:
        senders_list = ", ".join(list(other_senders)[:3])
        other_replies_desc = f"{other_since} replies from {senders_list} since user's last message"
    else:
        other_replies_desc = "No new replies since user's last message"

    # Summary
    summary = (
        f"Thread is {age_label} with {total} messages. "
        f"User has contributed {user_count} ({participation:.0%} participation). "
        f"{other_replies_desc}."
    )

    return {
        "subject": subject,
        "total_messages": total,
        "user_messages": user_count,
        "participation_rate": round(participation, 3),
        "user_initiated": user_initiated,
        "user_avg_body_length": round(avg_body_len),
        "user_engagement": engagement,
        "thread_duration_days": duration_days,
        "messages_today": messages_today,
        "other_replies_since": other_since,
        "other_replies_summary": other_summary,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Score explanation
# ---------------------------------------------------------------------------

def _build_score_explanation(factors, raw_score, calibrated_prob):
    """Render scoring factors as natural language, top 3 by magnitude."""
    if not factors:
        return f"Raw score {raw_score:.3f} → calibrated {calibrated_prob:.1%}"

    # Parse and sort factors by deviation from 1.0
    parsed = []
    for f in factors:
        if "=" in f:
            name, val_str = f.split("=", 1)
            try:
                val = float(val_str)
            except ValueError:
                parsed.append((name, val_str, 0))
                continue
            deviation = abs(val - 1.0) if val > 0.001 else abs(val)
            direction = "increases likelihood" if val > 1.0 else "decreases likelihood"
            parsed.append((name.replace("_", " "), direction, deviation))
        else:
            parsed.append((f, "", 0))

    # Sort by magnitude (most impactful first), take top 3
    parsed.sort(key=lambda x: x[2], reverse=True)
    top = parsed[:3]

    key_factors = ", ".join(
        f"{name} ({direction})" if direction else name
        for name, direction, _ in top
    )

    return (
        f"Calibrated probability: {calibrated_prob:.1%}. "
        f"Key factors: {key_factors}"
    )


# ---------------------------------------------------------------------------
# Feature checks (questions for Haiku to verify)
# ---------------------------------------------------------------------------

_FEATURE_CHECK_MAP = {
    "user_position_TO": {
        True: "User is in TO field — is the email directly addressed to them?",
        False: "User is CC'd — does the email still require their response?",
    },
    "user_mentioned": {
        True: "User mentioned by name — verify: is this a direct request or just a reference?",
        False: "User not mentioned by name — is there an implicit request directed at them?",
    },
    "has_question": {
        True: "Email contains a question mark — is an actual question being asked of the user?",
        False: "No question marks detected — check for implicit requests or action items.",
    },
    "has_action_language": {
        True: "Action language detected (e.g. 'please', 'could you') — is a task being assigned to the user?",
        False: "No action language detected — could there still be an implied expectation?",
    },
    "is_forward": {
        True: "This is a forwarded message — is the user expected to act on it or just FYI?",
        False: "Direct/reply message — standard response evaluation applies.",
    },
    "thread_user_initiated": {
        True: "User started this thread — are they being asked a follow-up?",
        False: "Thread started by someone else — is the user being pulled into this conversation?",
    },
}


def _build_feature_checks(email_data, signals):
    """Map fired/unfired features to verification questions for Haiku."""
    checks = []

    # User position
    in_to = signals.get("user_position") == "TO"
    check = _FEATURE_CHECK_MAP["user_position_TO"][in_to]
    if check:
        checks.append(check)

    # User mentioned
    mentioned = signals.get("user_mentioned_by_name", False)
    check = _FEATURE_CHECK_MAP["user_mentioned"][mentioned]
    if check:
        checks.append(check)

    # Question
    body = (email_data.get("body") or "")[:2000]
    has_q = "?" in body
    check = _FEATURE_CHECK_MAP["has_question"][has_q]
    if check:
        checks.append(check)

    # Action language
    has_action = signals.get("intent_category") == "direct_request"
    check = _FEATURE_CHECK_MAP["has_action_language"][has_action]
    if check:
        checks.append(check)

    # Forward
    subject_type = signals.get("subject_type", "")
    is_fwd = subject_type in ("forward", "chain_forward")
    check = _FEATURE_CHECK_MAP["is_forward"][is_fwd]
    if check:
        checks.append(check)

    # Thread user initiated
    # (this is populated from thread_info in the pipeline, not signals)
    # Will be added by caller if available

    return checks


# ---------------------------------------------------------------------------
# Anomaly flags
# ---------------------------------------------------------------------------

def _build_anomaly_flags(email_data, signals, contact):
    """Compare email properties to sender's historical averages (2σ deviation)."""
    flags = []

    total_recip = signals.get("total_recipients", 1)
    epm = contact.get("emails_per_month", 0) or 0
    body_len = len(email_data.get("body") or "")

    # 2σ recipient count check
    avg_recip = contact.get("avg_recipients")
    std_recip = contact.get("std_recipients")
    if avg_recip is not None and std_recip is not None and std_recip > 0:
        if total_recip > avg_recip + 2 * std_recip:
            flags.append(
                f"Unusually large recipient list ({total_recip} vs "
                f"avg {avg_recip:.0f} ± {std_recip:.0f})"
            )
    elif total_recip >= 8 and epm < 5:
        flags.append("Unusually large recipient list for a low-volume sender")

    # 2σ body length check
    avg_body = contact.get("avg_body_length")
    std_body = contact.get("std_body_length")
    if avg_body is not None and std_body is not None and std_body > 0:
        if body_len > avg_body + 2 * std_body:
            flags.append(
                f"Longer than typical ({body_len} chars vs "
                f"avg {avg_body:.0f} ± {std_body:.0f})"
            )
    elif body_len > 3000 and epm > 5:
        flags.append("Longer than typical email from this sender")

    # CC when normally TO
    if signals.get("user_position") == "CC":
        rate = contact.get("response_rate")
        if rate is not None and rate > 0.4:
            flags.append("User CC'd but has high reply rate with this sender — unusual")

    # High importance
    importance = email_data.get("importance", 1)
    if importance == 2:
        flags.append("Email marked as High Importance")

    # Unusual send time (if sender's active hours are known)
    sender_active_hours = contact.get("active_hours")
    if sender_active_hours and email_data.get("received_time"):
        try:
            ts = _parse_ts(email_data["received_time"])
            if ts and ts.hour not in sender_active_hours:
                flags.append(f"Sent at unusual hour ({ts.hour}:00) for this sender")
        except Exception:
            pass

    return flags


# ---------------------------------------------------------------------------
# Archetype prediction
# ---------------------------------------------------------------------------

def _predict_archetype(contact):
    """Predict the user's likely response archetype based on contact history.

    Heuristic:
    - avg body < 30 words → "acknowledgment" (short replies like "got it")
    - avg body > 150 words → "substantive" (detailed replies)
    - frequent forwarder → "routing"
    - else → "standard_reply"
    """
    if not contact:
        return "standard_reply"

    # Use avg_response_body_length if available, otherwise fall back
    avg_body = contact.get("avg_response_body_length")
    if avg_body is not None:
        # Convert chars to approximate words (5 chars/word)
        avg_words = avg_body / 5
        if avg_words < 30:
            return "acknowledgment"
        if avg_words > 150:
            return "substantive"

    return "standard_reply"


# ---------------------------------------------------------------------------
# Time pressure detection
# ---------------------------------------------------------------------------

_TIME_PRESSURE_PATTERNS = re.compile(
    r'\b(?:'
    r'asap|urgent|urgently|time.?sensitive|by eod|by end of day|'
    r'by cob|by close of business|deadline|due date|'
    r'(?:by|before|no later than)\s+(?:monday|tuesday|wednesday|thursday|friday|'
    r'saturday|sunday|tomorrow|tonight|this afternoon|this morning|noon|midnight)|'
    r'(?:need|needed|required)\s+(?:by|before|asap|today|tomorrow)'
    r')\b',
    re.IGNORECASE,
)


def _detect_time_pressure(email_data):
    """Detect deadline or urgency language in the email."""
    body = (email_data.get("body") or "")[:3000]
    subject = email_data.get("subject") or ""
    text = f"{subject} {body}"

    match = _TIME_PRESSURE_PATTERNS.search(text)
    if match:
        # Extract context around the match
        start = max(0, match.start() - 20)
        end = min(len(text), match.end() + 30)
        return text[start:end].strip()

    return None


# ---------------------------------------------------------------------------
# Message selection
# ---------------------------------------------------------------------------

def _select_messages(email_data, thread_messages, user_aliases):
    """Select key messages for Haiku: inbound, user's last, thread opener."""
    body = email_data.get("body") or ""

    inbound = {
        "sender": email_data.get("sender_name") or email_data.get("sender_email", ""),
        "received_time": email_data.get("received_time"),
        "body": body[:2000],
    }

    user_last = None
    thread_opener = None

    if thread_messages:
        sorted_msgs = sorted(
            thread_messages, key=lambda m: m.get("received_time") or ""
        )

        # User's last reply
        user_msgs = [
            m for m in sorted_msgs
            if (m.get("sender_email") or "").lower() in user_aliases
        ]
        if user_msgs:
            last = user_msgs[-1]
            user_last = {
                "sender": "User",
                "received_time": last.get("received_time"),
                "body": (last.get("body") or "")[:1000],
            }

        # Thread opener (first message, truncated)
        if sorted_msgs:
            opener = sorted_msgs[0]
            opener_sender = (opener.get("sender_email") or "").lower()
            # Only include if different from inbound
            if opener.get("received_time") != email_data.get("received_time"):
                thread_opener = {
                    "sender": opener.get("sender_name") or opener_sender,
                    "received_time": opener.get("received_time"),
                    "body": (opener.get("body") or "")[:500],
                }

    return {
        "inbound": inbound,
        "user_last": user_last,
        "thread_opener": thread_opener,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str):
    """Parse ISO timestamp to timezone-aware datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
