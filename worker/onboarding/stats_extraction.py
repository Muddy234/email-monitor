"""Full statistical extraction for onboarding (replaces statistics.py).

Produces five output tables from raw email data:
  - response_events: per-inbound-email labeling (responded, latency, features)
  - contacts: per-sender statistics (reply rates, latency, relationship)
  - threads: per-conversation statistics (participation, body length)
  - domains: per-domain aggregate statistics
  - user_profile: overall user behavior profile

No LLM calls. All computation is pure Python.
"""

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

logger = logging.getLogger("worker.onboarding")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BAYESIAN_PRIOR_WEIGHT = 3
LATENCY_CAP_HRS = 168  # 7 days
RECURRING_CV_THRESHOLD = 0.5
MIN_CADENCE_OBSERVATIONS = 3

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

_STRIP_PATTERNS = re.compile(
    r'\b(?:\d{1,2}[/\-]\d{1,2}[/\-]?\d{0,4}|'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d+'
    r'|\d{4}|#\d+|v\d+)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_all(emails, user_aliases):
    """Run full extraction pipeline.

    Args:
        emails: List of all email dicts (received + sent, from Supabase).
        user_aliases: List of user's email addresses (lowercase).

    Returns:
        dict with keys: response_events, contacts, threads, domains, user_profile
    """
    aliases = {a.lower() for a in user_aliases} if user_aliases else set()

    received, sent = _split_emails(emails, aliases)
    logger.info(f"Extraction: {len(received)} received, {len(sent)} sent")

    # Step 1: Build response events with fan-out fix
    response_events = _build_response_events(received, sent, aliases)

    global_responded = sum(1 for e in response_events if e.get("responded"))
    global_total = len(response_events)
    global_rate = global_responded / max(global_total, 1)
    logger.info(f"Response events: {global_responded}/{global_total} = {global_rate:.4f}")

    # Step 2: Build contacts
    contacts = _build_contacts(response_events, global_rate, aliases)

    # Step 3: Build threads
    threads = _build_threads(emails, aliases)

    # Step 3b: Enrich contacts with user_initiates_pct from threads
    _enrich_user_initiates_pct(contacts, threads, response_events)

    # Step 3c: Enrich response events with thread-level features
    _enrich_thread_features(response_events, threads)

    # Step 4: Build domains
    domains = _build_domains(contacts)

    # Step 5: Build user profile
    user_profile = _build_user_profile(received, sent, global_rate)

    # Step 5b: Enrich response events with active-time features
    _enrich_active_time_features(response_events, user_profile)

    return {
        "response_events": response_events,
        "contacts": contacts,
        "threads": threads,
        "domains": domains,
        "user_profile": user_profile,
    }


# ---------------------------------------------------------------------------
# Step 1: Response events
# ---------------------------------------------------------------------------

def _split_emails(emails, user_aliases):
    """Split emails into received and sent based on folder or sender."""
    received = []
    sent = []
    for email in emails:
        folder = (email.get("folder") or "").lower()
        sender = (email.get("sender_email") or email.get("sender") or "").lower()
        if folder == "sent items" or sender in user_aliases:
            sent.append(email)
        else:
            received.append(email)
    return received, sent


def _build_response_events(received, sent, user_aliases):
    """Build response event records with three-tier matching + fan-out fix."""
    # Derive user name tokens and domains from aliases for feature extraction
    user_name_tokens = set()
    user_domains = set()
    for alias in user_aliases:
        if "@" in alias:
            local, domain = alias.split("@", 1)
            user_domains.add(domain.lower())
            for part in re.split(r'[._\-]', local):
                if len(part) >= 3:
                    user_name_tokens.add(part.lower())

    # Index sent emails
    sent_by_conv = defaultdict(list)
    sent_by_recipient = defaultdict(list)

    for s in sent:
        conv_id = s.get("conversation_id")
        if conv_id:
            sent_by_conv[conv_id].append(s)

        # Index by recipients
        recipients = s.get("recipients") or []
        if isinstance(recipients, list):
            for r in recipients:
                addr = (r.get("email") or r.get("address") or "").lower()
                if addr:
                    sent_by_recipient[addr].append(s)

        # Also parse to_field/cc_field strings
        for field in ("to_field", "cc_field"):
            raw = s.get(field) or ""
            for addr in _EMAIL_RE.findall(raw):
                sent_by_recipient[addr.lower()].append(s)

    # Match each received email to a response
    events = []
    for email in received:
        sender = (email.get("sender_email") or email.get("sender") or "").lower()
        if not sender or sender in user_aliases:
            continue

        received_time = _parse_time(email.get("received_time"))
        response = _find_response(email, received_time, sender,
                                  sent_by_conv, sent_by_recipient, user_aliases)

        responded = response is not None
        latency = None
        response_type = None
        response_msg_id = None

        if response and received_time:
            response_time = _parse_time(response.get("received_time"))
            if response_time and response_time > received_time:
                hours = (response_time - received_time).total_seconds() / 3600
                if hours < LATENCY_CAP_HRS:
                    latency = round(hours, 2)

            response_type = _classify_response_type(response)
            response_msg_id = response.get("email_ref") or response.get("id")

        # Re-label forwards: forward responses don't count as "responded"
        if responded and response_type == "forward":
            responded = False

        # Extract features
        body = email.get("body") or ""
        subject = email.get("subject") or ""
        user_position = _detect_user_position(email, user_aliases)
        total_recipients = _count_recipients(email)

        # Detect user name mentions in body (before quoted text)
        mentions_user_name = False
        if user_name_tokens:
            check_body = body
            for marker in ("From:", "-----Original Message", "________________________________"):
                idx = check_body.find(marker)
                if idx > 0:
                    check_body = check_body[:idx]
                    break
            for token in user_name_tokens:
                if re.search(rf'\b{re.escape(token)}\b', check_body[:2000], re.IGNORECASE):
                    mentions_user_name = True
                    break

        # Check if sender is from the same domain as user
        sender_domain = sender.split("@")[1] if "@" in sender else ""
        sender_is_internal = sender_domain in user_domains

        event = {
            "email_id": email.get("id") or email.get("email_ref") or "",
            "sender_email": sender,
            "received_time": email.get("received_time"),
            "responded": responded,
            "response_latency_hours": latency,
            "response_type": response_type,
            "conversation_id": email.get("conversation_id"),
            "subject": subject[:200],
            "user_position": user_position,
            "total_recipients": total_recipients,
            "has_question": "?" in body[:2000],
            "has_action_language": _has_action_language(body),
            "subject_type": _classify_subject_type(subject),
            "is_recurring": False,  # Set in _detect_recurring below
            "mentions_user_name": mentions_user_name,
            "sender_is_internal": sender_is_internal,
            "response_msg_id": response_msg_id,
        }
        events.append(event)

    # Fan-out fix: only most recent inbound before each reply keeps responded=true
    _fix_fanout(events)

    # Detect recurring patterns and mark events
    _detect_recurring(events)

    return events


def _find_response(email, received_time, sender, sent_by_conv,
                   sent_by_recipient, user_aliases):
    """Three-tier response matching."""
    conv_id = email.get("conversation_id")

    # Tier 1: conversation_id match — closest sent after received
    if conv_id and conv_id in sent_by_conv:
        best = None
        best_delta = None
        for s in sent_by_conv[conv_id]:
            sent_time = _parse_time(s.get("received_time"))
            if sent_time and received_time and sent_time > received_time:
                delta = (sent_time - received_time).total_seconds()
                if delta < 48 * 3600 and (best_delta is None or delta < best_delta):
                    best = s
                    best_delta = delta
        if best:
            return best

    # Tier 2: Subject similarity + sent to sender within 48h — closest match
    if received_time and sender:
        subject_clean = _normalize_subject(email.get("subject"))
        if sender in sent_by_recipient:
            best = None
            best_delta = None
            for s in sent_by_recipient[sender]:
                s_clean = _normalize_subject(s.get("subject"))
                if subject_clean and s_clean and _subject_similar(subject_clean, s_clean):
                    sent_time = _parse_time(s.get("received_time"))
                    if sent_time and sent_time > received_time:
                        delta = (sent_time - received_time).total_seconds()
                        if delta < 48 * 3600 and (best_delta is None or delta < best_delta):
                            best = s
                            best_delta = delta
            if best:
                return best

    # Tier 3: Sent to sender within 4h (loose) — closest match
    if received_time and sender and sender in sent_by_recipient:
        best = None
        best_delta = None
        for s in sent_by_recipient[sender]:
            sent_time = _parse_time(s.get("received_time"))
            if sent_time and sent_time > received_time:
                delta = (sent_time - received_time).total_seconds()
                if delta < 4 * 3600 and (best_delta is None or delta < best_delta):
                    best = s
                    best_delta = delta
        if best:
            return best

    return None


def _fix_fanout(events):
    """Fix fan-out: only the most recent inbound before each reply keeps responded=true."""
    by_response = defaultdict(list)
    for ev in events:
        resp_id = ev.get("response_msg_id")
        if ev.get("responded") and resp_id:
            by_response[resp_id].append(ev)

    demoted = 0
    for resp_id, group in by_response.items():
        if len(group) <= 1:
            continue
        group.sort(
            key=lambda e: _parse_time(e.get("received_time")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        for ev in group[1:]:
            ev["responded"] = False
            demoted += 1

    if demoted:
        logger.info(f"Fan-out fix: demoted {demoted} events")


def _detect_recurring(events):
    """Detect recurring email patterns and mark events."""
    groups = defaultdict(list)
    for ev in events:
        norm = _normalize_subject(ev.get("subject"))
        if norm:
            key = f"{ev['sender_email']}|{norm}"
            groups[key].append(ev)

    patterns_found = 0
    for key, group in groups.items():
        if len(group) < MIN_CADENCE_OBSERVATIONS:
            continue

        timestamps = sorted(
            t for t in (_parse_time(e.get("received_time")) for e in group) if t
        )
        if len(timestamps) < MIN_CADENCE_OBSERVATIONS:
            continue

        gaps = [
            (timestamps[i + 1] - timestamps[i]).total_seconds() / 3600
            for i in range(len(timestamps) - 1)
        ]
        if not gaps:
            continue

        mean_gap = sum(gaps) / len(gaps)
        if mean_gap <= 0:
            continue

        std_gap = (sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) ** 0.5
        cv = std_gap / mean_gap

        if cv < RECURRING_CV_THRESHOLD:
            patterns_found += 1
            for ev in group:
                ev["is_recurring"] = True

    if patterns_found:
        logger.info(f"Recurring patterns: {patterns_found} detected")


# ---------------------------------------------------------------------------
# Step 2: Contacts
# ---------------------------------------------------------------------------

def _build_contacts(response_events, global_rate, user_aliases):
    """Build per-sender contact statistics from response events."""
    by_sender = defaultdict(list)
    for ev in response_events:
        by_sender[ev["sender_email"]].append(ev)

    contacts = {}
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    ninety_days_ago = now - timedelta(days=90)

    # Global observation span — used as denominator for emails_per_month
    # so bursty senders don't get inflated rates.
    all_timestamps = [t for t in (_parse_time(e.get("received_time"))
                                  for e in response_events) if t]
    if len(all_timestamps) >= 2:
        obs_span_days = max((max(all_timestamps) - min(all_timestamps)).days, 1)
    else:
        obs_span_days = 30

    for sender, events in by_sender.items():
        total = len(events)
        responded = [e for e in events if e.get("responded")]
        total_responded = len(responded)

        reply_rate = total_responded / total if total > 0 else 0

        # Time-windowed rates
        events_30d = [e for e in events if _is_after(e.get("received_time"), thirty_days_ago)]
        events_90d = [e for e in events if _is_after(e.get("received_time"), ninety_days_ago)]
        rate_30d = (sum(1 for e in events_30d if e.get("responded")) / len(events_30d)
                    if events_30d else None)
        rate_90d = (sum(1 for e in events_90d if e.get("responded")) / len(events_90d)
                    if events_90d else None)

        # Bayesian smoothed rate
        smoothed_rate = (
            (total_responded + BAYESIAN_PRIOR_WEIGHT * global_rate)
            / (total + BAYESIAN_PRIOR_WEIGHT)
        )

        # Response times
        latencies = [e["response_latency_hours"] for e in responded
                     if e.get("response_latency_hours") is not None]
        avg_response_time = round(sum(latencies) / len(latencies), 2) if latencies else None
        median_response_time = round(median(latencies), 2) if latencies else None

        # Forwards
        forward_count = sum(1 for e in events
                            if e.get("subject_type") in ("forward", "chain_forward"))
        forward_rate = forward_count / total if total > 0 else 0

        # Date range
        timestamps = [t for t in (_parse_time(e.get("received_time")) for e in events) if t]
        first_seen = min(timestamps).isoformat() if timestamps else None
        last_seen = max(timestamps).isoformat() if timestamps else None

        # Emails per month (normalized by full observation window)
        emails_per_month = round(total * 30 / obs_span_days, 1)

        # Contact type — use sender_is_internal from response events
        domain = sender.split("@")[1] if "@" in sender else "unknown"
        is_internal = any(e.get("sender_is_internal") for e in events)
        contact_type = "internal_colleague" if is_internal else "external"

        # Typical subjects
        subject_counter = Counter()
        for e in events:
            norm = _normalize_subject(e.get("subject"))
            if norm:
                subject_counter[norm] += 1
        typical_subjects = [s for s, _ in subject_counter.most_common(3)]

        contacts[sender] = {
            "email": sender,
            "total_received": total,
            "total_responded": total_responded,
            "reply_rate": round(reply_rate, 4),
            "reply_rate_30d": round(rate_30d, 4) if rate_30d is not None else None,
            "reply_rate_90d": round(rate_90d, 4) if rate_90d is not None else None,
            "smoothed_rate": round(smoothed_rate, 4),
            "avg_response_time_hours": avg_response_time,
            "median_response_time_hours": median_response_time,
            "user_initiates_pct": None,  # Enriched by _enrich_user_initiates_pct
            "co_recipients_top5": [],
            "forward_count": forward_count,
            "forward_rate": round(forward_rate, 4),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "emails_per_month": emails_per_month,
            "contact_type": contact_type,
            "typical_subjects": typical_subjects,
        }

    return contacts


# ---------------------------------------------------------------------------
# Step 3: Threads
# ---------------------------------------------------------------------------

def _build_threads(emails, user_aliases):
    """Build per-thread statistics from all emails."""
    by_conv = defaultdict(list)
    for email in emails:
        conv_id = email.get("conversation_id")
        if conv_id:
            by_conv[conv_id].append(email)

    threads = {}
    for conv_id, msgs in by_conv.items():
        msgs.sort(
            key=lambda m: _parse_time(m.get("received_time")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        total = len(msgs)

        user_msgs = []
        other_responders = set()
        for m in msgs:
            sender = (m.get("sender_email") or m.get("sender") or "").lower()
            if sender in user_aliases:
                user_msgs.append(m)
            elif sender:
                other_responders.add(sender)

        user_count = len(user_msgs)
        participation = user_count / total if total > 0 else 0

        # User initiated?
        first_sender = ""
        if msgs:
            first_sender = (msgs[0].get("sender_email") or
                            msgs[0].get("sender") or "").lower()
        user_initiated = first_sender in user_aliases

        # User avg body length
        user_bodies = [len(m.get("body") or "") for m in user_msgs]
        user_avg_body = round(sum(user_bodies) / len(user_bodies)) if user_bodies else 0

        # Duration
        timestamps = [t for t in (_parse_time(m.get("received_time")) for m in msgs) if t]
        if len(timestamps) >= 2:
            duration_days = max((max(timestamps) - min(timestamps)).days, 0)
        else:
            duration_days = 0

        threads[conv_id] = {
            "conversation_id": conv_id,
            "total_messages": total,
            "user_messages": user_count,
            "participation_rate": round(participation, 4),
            "user_initiated": user_initiated,
            "user_avg_body_length": float(user_avg_body),
            "other_responders": list(other_responders)[:10],
            "duration_days": duration_days,
        }

    return threads


def _enrich_user_initiates_pct(contacts, threads, response_events):
    """Set user_initiates_pct on each contact using thread data.

    For each sender, find the conversations they appear in, check how many
    the user initiated, and store the percentage.
    """
    # Map sender → set of conversation_ids they participate in
    sender_convs = defaultdict(set)
    for ev in response_events:
        conv_id = ev.get("conversation_id")
        if conv_id:
            sender_convs[ev["sender_email"]].add(conv_id)

    for sender, contact in contacts.items():
        conv_ids = sender_convs.get(sender, set())
        if not conv_ids:
            continue

        initiated = sum(
            1 for cid in conv_ids
            if cid in threads and threads[cid].get("user_initiated")
        )
        contact["user_initiates_pct"] = round(initiated / len(conv_ids), 4)


def _enrich_thread_features(response_events, threads):
    """Add thread_user_initiated and thread_depth to response events."""
    for ev in response_events:
        conv_id = ev.get("conversation_id")
        if conv_id and conv_id in threads:
            thread = threads[conv_id]
            ev["thread_user_initiated"] = thread.get("user_initiated", False)
            ev["thread_depth"] = thread.get("total_messages", 1)
        else:
            ev["thread_user_initiated"] = False
            ev["thread_depth"] = 1


def _enrich_active_time_features(response_events, user_profile):
    """Add arrived_during_active_hours and arrived_on_active_day."""
    raw_hours = user_profile.get("active_hours", [])
    raw_days = user_profile.get("active_days", [])

    # Build full active-hour set: expand top-3 peak hours to +/-1 range,
    # fall back to 8-18 business hours if no profile data.
    if raw_hours:
        active_hours = set()
        for h in raw_hours:
            active_hours.update(range(max(0, h - 1), min(24, h + 2)))
    else:
        active_hours = set(range(8, 18))

    active_days = set(raw_days) if raw_days else {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    }

    for ev in response_events:
        t = _parse_time(ev.get("received_time"))
        if t:
            ev["arrived_during_active_hours"] = t.hour in active_hours
            ev["arrived_on_active_day"] = t.strftime("%A") in active_days
        else:
            ev["arrived_during_active_hours"] = None
            ev["arrived_on_active_day"] = None


# ---------------------------------------------------------------------------
# Step 4: Domains
# ---------------------------------------------------------------------------

def _build_domains(contacts):
    """Build per-domain statistics from contacts."""
    by_domain = defaultdict(list)
    for sender, data in contacts.items():
        domain = sender.split("@")[1] if "@" in sender else "unknown"
        by_domain[domain].append(data)

    domains = {}
    for domain, contact_list in by_domain.items():
        rates = [c["reply_rate"] for c in contact_list if c.get("reply_rate") is not None]
        avg_rate = sum(rates) / len(rates) if rates else 0

        domains[domain] = {
            "domain": domain,
            "avg_reply_rate": round(avg_rate, 4),
            "contact_count": len(contact_list),
            "domain_category": "external",  # Refined by caller
        }

    return domains


# ---------------------------------------------------------------------------
# Step 5: User profile
# ---------------------------------------------------------------------------

def _build_user_profile(received, sent, global_rate):
    """Build overall user behavior profile."""
    hour_counts = Counter()
    day_counts = Counter()

    for email in sent:
        t = _parse_time(email.get("received_time"))
        if t:
            hour_counts[t.hour] += 1
            day_counts[t.strftime("%A")] += 1

    active_hours = [h for h, _ in hour_counts.most_common(3)]

    total_day = sum(day_counts.values())
    active_days = [d for d, c in day_counts.items()
                   if total_day > 0 and c / total_day > 0.10]

    # Message type breakdown from received
    type_counter = Counter()
    for email in received:
        msg_type = _classify_subject_type(email.get("subject") or "")
        type_counter[msg_type] += 1
    total_typed = sum(type_counter.values())
    message_type_pct = {
        t: round(c / total_typed, 4) for t, c in type_counter.items()
    } if total_typed > 0 else {}

    return {
        "active_hours": active_hours,
        "active_days": active_days,
        "overall_reply_rate": round(global_rate, 4),
        "message_type_pct": message_type_pct,
        "total_received": len(received),
        "total_sent": len(sent),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time(value):
    """Parse a datetime string to a timezone-aware datetime object."""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError, TypeError):
        return None


def _is_after(ts_str, threshold_dt):
    """Check if a timestamp string is after a threshold datetime."""
    dt = _parse_time(ts_str)
    if not dt:
        return False
    return dt > threshold_dt


def _normalize_subject(subject):
    """Normalize subject for recurring pattern matching."""
    if not subject:
        return ""
    s = re.sub(r'^(?:re|fw|fwd)\s*:\s*', '', str(subject), flags=re.IGNORECASE).strip()
    s = _STRIP_PATTERNS.sub('', s).strip()
    s = re.sub(r'\s+', ' ', s).lower()
    return s


def _subject_similar(a, b):
    """Check if two normalized subjects are similar enough (>=0.6 ratio)."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(len(a), len(b))
    longer = max(len(a), len(b))
    if longer == 0:
        return True
    if shorter / longer >= 0.6 and a[:min(20, shorter)] == b[:min(20, shorter)]:
        return True
    return False


def _detect_user_position(email, user_aliases):
    """Detect if user is in TO or CC field."""
    # Check structured recipients list first
    recipients = email.get("recipients") or []
    if isinstance(recipients, list):
        for r in recipients:
            addr = (r.get("email") or r.get("address") or "").lower()
            rtype = r.get("type")
            if addr in user_aliases:
                # Handle both int (1=TO,2=CC,3=BCC) and string types
                if rtype in (2, 3, "CC", "BCC", "Cc", "Bcc"):
                    return "CC"
                return "TO"

    # Fallback to string matching
    to_field = (email.get("to_field") or "").lower()
    cc_field = (email.get("cc_field") or "").lower()
    for alias in user_aliases:
        if alias in cc_field:
            return "CC"
        if alias in to_field:
            return "TO"

    return "TO"  # Default


def _count_recipients(email):
    """Count total recipients."""
    recipients = email.get("recipients") or []
    if isinstance(recipients, list) and recipients:
        return len(recipients)

    count = 0
    for field in ("to_field", "cc_field"):
        raw = email.get(field) or ""
        count += len(_EMAIL_RE.findall(raw))
    return max(count, 1)


def _has_action_language(body):
    """Detect action-oriented language in email body."""
    if not body:
        return False
    lower = body[:2000].lower()
    patterns = [
        r'\bplease\s+(?:review|approve|confirm|sign|send|provide|update|let\s+me\s+know)',
        r'\bcould\s+you\b',
        r'\bcan\s+you\b',
        r'\bneed\s+(?:you|your)\b',
        r'\baction\s+required\b',
        r'\bplease\s+(?:advise|respond)\b',
    ]
    for p in patterns:
        if re.search(p, lower):
            return True
    return False


def _classify_subject_type(subject):
    """Classify email subject type."""
    if not subject:
        return "new"
    lower = subject.strip().lower()
    if re.match(r'^re\s*:', lower):
        if re.match(r'^re\s*:.*fw[d]?\s*:', lower):
            return "chain_forward"
        return "reply"
    if re.match(r'^fw[d]?\s*:', lower):
        return "forward"
    return "new"


def _classify_response_type(sent_email):
    """Classify how the user responded (reply, forward, etc.)."""
    subject = (sent_email.get("subject") or "").strip().lower()
    if re.match(r'^fw[d]?\s*:', subject):
        return "forward"
    return "reply"
