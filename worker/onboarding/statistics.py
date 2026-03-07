"""Phase 2: Pure-Python statistics computation for onboarding.

No LLM calls. Computes contact frequencies, response rates, threading,
aggregate stats, and subject token frequencies from raw email data.
"""

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime

logger = logging.getLogger("worker.onboarding")


def compute_all_statistics(received, sent, user_aliases):
    """Run all Phase 2 sub-computations.

    Args:
        received: List of received email dicts.
        sent: List of sent email dicts.
        user_aliases: List of user's email addresses (lowercase).

    Returns:
        dict with sub-results keyed by phase name.
    """
    aliases = {a.lower() for a in user_aliases} if user_aliases else set()

    contact_freq = compute_contact_frequencies(received)
    response_rates = compute_response_rates(received, sent, aliases)
    aggregate = compute_aggregate_stats(received, sent)
    threading = compute_threading_stats(received, sent, aliases)
    tokens = extract_subject_tokens(received)

    return {
        "contact_frequencies": contact_freq,
        "response_rates": response_rates,
        "aggregate": aggregate,
        "threading": threading,
        "subject_tokens": tokens,
    }


def compute_contact_frequencies(received):
    """Phase 2A: Per-sender frequency and metadata.

    Returns:
        dict[str, dict]: Keyed by sender email (lowercase).
    """
    contacts = defaultdict(lambda: {
        "count": 0,
        "first_seen": None,
        "last_seen": None,
        "subjects": [],
        "to_count": 0,
        "cc_count": 0,
        "co_recipients": Counter(),
    })

    for email in received:
        sender = (email.get("sender_email") or email.get("sender") or "").lower()
        if not sender:
            continue

        c = contacts[sender]
        c["count"] += 1

        received_time = email.get("received_time")
        if received_time:
            if c["first_seen"] is None or received_time < c["first_seen"]:
                c["first_seen"] = received_time
            if c["last_seen"] is None or received_time > c["last_seen"]:
                c["last_seen"] = received_time

        subject = email.get("subject") or ""
        if subject and len(c["subjects"]) < 10:
            c["subjects"].append(subject)

        # Check if user was in TO or CC
        to_field = (email.get("to_field") or "").lower()
        cc_field = (email.get("cc_field") or "").lower()
        # Simple heuristic — if any alias appears in to, count as TO
        c["to_count"] += 1  # Default: all received are "to" the user
        if cc_field:
            c["cc_count"] += 1

        # Co-recipients from recipients jsonb
        recipients = email.get("recipients") or []
        if isinstance(recipients, list):
            for r in recipients:
                addr = (r.get("email") or r.get("address") or "").lower()
                if addr and addr != sender:
                    c["co_recipients"][addr] += 1

    # Serialize for JSON compatibility
    result = {}
    for sender, data in contacts.items():
        top_co = [addr for addr, _ in data["co_recipients"].most_common(5)]
        result[sender] = {
            "count": data["count"],
            "first_seen": data["first_seen"],
            "last_seen": data["last_seen"],
            "subjects": data["subjects"],
            "to_count": data["to_count"],
            "cc_count": data["cc_count"],
            "top_co_recipients": top_co,
        }

    return result


def compute_response_rates(received, sent, user_aliases):
    """Phase 2B: Per-sender response rate and avg response time.

    Uses three-tier matching:
    1. conversation_id match
    2. Subject similarity + time window
    3. Recipient + time window fallback

    Returns:
        dict[str, dict]: Per-sender response_rate and avg_response_time_hours.
    """
    # Index sent emails by conversation_id and by recipient
    sent_by_conv = defaultdict(list)
    sent_by_recipient = defaultdict(list)

    for s in sent:
        conv_id = s.get("conversation_id")
        if conv_id:
            sent_by_conv[conv_id].append(s)

        # Index by all recipients
        for field in ("to_field", "cc_field"):
            raw = s.get(field) or ""
            for addr in _extract_emails(raw):
                sent_by_recipient[addr.lower()].append(s)

    # Match received emails to responses
    per_sender = defaultdict(lambda: {"total": 0, "responded": 0, "response_times": []})

    for email in received:
        sender = (email.get("sender_email") or email.get("sender") or "").lower()
        if not sender:
            continue

        per_sender[sender]["total"] += 1
        received_time = _parse_time(email.get("received_time"))
        if not received_time:
            continue

        response = _find_response(email, sent_by_conv, sent_by_recipient, user_aliases)
        if response:
            per_sender[sender]["responded"] += 1
            response_time = _parse_time(response.get("received_time"))
            if response_time and response_time > received_time:
                hours = (response_time - received_time).total_seconds() / 3600
                if hours < 168:  # Cap at 1 week
                    per_sender[sender]["response_times"].append(hours)

    result = {}
    for sender, data in per_sender.items():
        rate = data["responded"] / data["total"] if data["total"] > 0 else 0
        avg_time = None
        if data["response_times"]:
            avg_time = sum(data["response_times"]) / len(data["response_times"])
        result[sender] = {
            "response_rate": round(rate, 4),
            "avg_response_time_hours": round(avg_time, 2) if avg_time else None,
            "total_received": data["total"],
            "total_responded": data["responded"],
        }

    return result


def compute_aggregate_stats(received, sent):
    """Phase 2C: Overall stats.

    Returns:
        dict with overall_response_rate, emails_per_day_avg,
        busiest_hour, busiest_day.
    """
    total_received = len(received)
    total_sent = len(sent)

    # Busiest hour and day
    hour_counts = Counter()
    day_counts = Counter()

    all_emails = received + sent
    dates = set()

    for email in all_emails:
        t = _parse_time(email.get("received_time"))
        if t:
            hour_counts[t.hour] += 1
            day_counts[t.strftime("%A")] += 1
            dates.add(t.date())

    num_days = len(dates) or 1
    busiest_hour = hour_counts.most_common(1)[0][0] if hour_counts else None
    busiest_day = day_counts.most_common(1)[0][0] if day_counts else None

    return {
        "total_received": total_received,
        "total_sent": total_sent,
        "emails_per_day_avg": round((total_received + total_sent) / num_days, 1),
        "busiest_hour": busiest_hour,
        "busiest_day": busiest_day,
        "date_range_days": num_days,
    }


def compute_threading_stats(received, sent, user_aliases):
    """Phase 2D: Threading / conversation statistics.

    Returns:
        dict with per-thread and aggregate threading stats.
    """
    threads = defaultdict(lambda: {
        "received": [],
        "sent": [],
        "participants": set(),
    })

    for email in received:
        conv_id = email.get("conversation_id") or email.get("subject", "")
        threads[conv_id]["received"].append(email)
        sender = (email.get("sender_email") or "").lower()
        if sender:
            threads[conv_id]["participants"].add(sender)

    for email in sent:
        conv_id = email.get("conversation_id") or email.get("subject", "")
        threads[conv_id]["sent"].append(email)

    # Aggregate
    thread_lengths = []
    user_participated = 0

    for conv_id, data in threads.items():
        total = len(data["received"]) + len(data["sent"])
        thread_lengths.append(total)
        if data["sent"]:
            user_participated += 1

    total_threads = len(threads)

    return {
        "total_threads": total_threads,
        "user_participated_count": user_participated,
        "user_participation_rate": round(
            user_participated / total_threads, 4
        ) if total_threads > 0 else 0,
        "avg_thread_length": round(
            sum(thread_lengths) / len(thread_lengths), 1
        ) if thread_lengths else 0,
        "max_thread_length": max(thread_lengths) if thread_lengths else 0,
    }


def extract_subject_tokens(received):
    """Phase 2E: Token and bigram frequency extraction from subjects.

    Returns:
        dict with token_counts and bigram_counts (top 100 each).
    """
    # Stop words to exclude
    stop_words = {
        "re", "fw", "fwd", "the", "a", "an", "and", "or", "but", "in", "on",
        "at", "to", "for", "of", "with", "by", "from", "is", "are", "was",
        "were", "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "will", "would", "could", "should", "may", "might", "can",
        "this", "that", "these", "those", "it", "its", "i", "you", "we",
        "they", "he", "she", "my", "your", "our", "their", "me", "us",
        "him", "her", "not", "no", "so", "if", "up", "out", "about",
    }

    token_counter = Counter()
    bigram_counter = Counter()

    for email in received:
        subject = (email.get("subject") or "").lower()
        # Remove Re:/Fw: prefixes
        subject = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", subject, flags=re.IGNORECASE)
        # Tokenize
        tokens = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", subject)
        meaningful = [t for t in tokens if t not in stop_words and len(t) > 1]

        for token in meaningful:
            token_counter[token] += 1

        # Bigrams
        for i in range(len(meaningful) - 1):
            bigram = f"{meaningful[i]} {meaningful[i+1]}"
            bigram_counter[bigram] += 1

    return {
        "token_counts": dict(token_counter.most_common(100)),
        "bigram_counts": dict(bigram_counter.most_common(100)),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _extract_emails(text):
    """Extract email addresses from a string."""
    return _EMAIL_RE.findall(text) if text else []


def _parse_time(value):
    """Parse a datetime string or return None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # ISO format from Supabase
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _find_response(email, sent_by_conv, sent_by_recipient, user_aliases):
    """Find the user's response to a received email using three-tier matching.

    Returns:
        The matching sent email dict, or None.
    """
    received_time = _parse_time(email.get("received_time"))
    conv_id = email.get("conversation_id")
    sender = (email.get("sender_email") or "").lower()

    # Tier 1: conversation_id match
    if conv_id and conv_id in sent_by_conv:
        for s in sent_by_conv[conv_id]:
            sent_time = _parse_time(s.get("received_time"))
            if sent_time and received_time and sent_time > received_time:
                return s

    # Tier 2: Same subject + sent to the sender within 48 hours
    if received_time and sender:
        subject = (email.get("subject") or "").lower()
        subject_clean = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", subject).strip()

        if sender in sent_by_recipient:
            for s in sent_by_recipient[sender]:
                s_subject = (s.get("subject") or "").lower()
                s_clean = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", s_subject).strip()
                if s_clean == subject_clean:
                    sent_time = _parse_time(s.get("received_time"))
                    if sent_time and sent_time > received_time:
                        hours = (sent_time - received_time).total_seconds() / 3600
                        if hours < 48:
                            return s

    # Tier 3: Sent to sender within 4 hours (loose match)
    if received_time and sender and sender in sent_by_recipient:
        for s in sent_by_recipient[sender]:
            sent_time = _parse_time(s.get("received_time"))
            if sent_time and sent_time > received_time:
                hours = (sent_time - received_time).total_seconds() / 3600
                if hours < 4:
                    return s

    return None
