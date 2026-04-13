"""Select 10 stratified reply-only test emails for calibration."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("worker.calibration")

TARGET_COUNT = 10

# Stratification bucket targets — keys match bucket names used below
BUCKET_TARGETS = {
    "internal_colleague": 4,
    "external_professional": 3,
    "external_casual": 2,
    "personal": 1,
    "standalone": 3,
    "mid_thread": 3,
    "complex_thread": 2,
    "material_consequence": 2,
    "action_request": 2,
    "user_bottleneck": 2,
}

# Contact types that map to each contact-type bucket
_CONTACT_TYPE_MAP = {
    "internal_colleague": {"internal", "colleague", "team_member"},
    "external_professional": {"external_professional", "lender", "legal",
                              "investor", "client", "vendor_professional"},
    "external_casual": {"external", "vendor", "contractor", "unknown"},
    "personal": {"personal", "friend", "family"},
}


@dataclass
class CalibrationEmail:
    db_id: str
    incoming_email: dict
    thread_emails: list
    user_reply: str | None
    contact_type: str
    thread_depth: int
    signal_scores: dict
    selection_buckets: list = field(default_factory=list)


def select_calibration_emails(db, user_id, aliases):
    """Select up to 10 reply-only test emails with stratified sampling.

    Args:
        db: SupabaseWorkerClient instance.
        user_id: UUID string.
        aliases: List of user email aliases.

    Returns:
        list[CalibrationEmail]: Selected test emails.
    """
    alias_set = {a.lower() for a in aliases} if aliases else set()
    logger.debug(f"[CAL-SEL] selecting calibration emails for {user_id[:8]}, aliases={len(alias_set)}")

    # Fetch recent received emails (last 30 days preferred, expand if needed)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    received = _fetch_received_emails(db, user_id, cutoff)
    logger.debug(f"[CAL-SEL] 30-day received: {len(received)}")

    if len(received) < TARGET_COUNT:
        cutoff_60 = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        received = _fetch_received_emails(db, user_id, cutoff_60)
        logger.debug(f"[CAL-SEL] expanded to 60-day: {len(received)}")

    if not received:
        logger.warning("No received emails found for calibration")
        return []

    # Fetch sent emails to find user replies
    sent_by_conv, sent_by_subject = _fetch_sent_emails_by_conversation(db, user_id, alias_set)
    logger.debug(f"[CAL-SEL] sent indexed: {len(sent_by_conv)} by conv_id, {len(sent_by_subject)} by subject")

    # Fetch contacts for contact type info
    sender_emails = list({r["sender_email"] for r in received if r.get("sender_email")})
    contacts = db.fetch_contacts_by_emails(user_id, sender_emails)
    logger.debug(f"[CAL-SEL] contacts fetched: {len(contacts)} of {len(sender_emails)} senders")

    # Fetch signal scores from response_events
    email_ids = [r["id"] for r in received]
    signals = _fetch_signal_scores(db, user_id, email_ids)

    # Build candidate pool
    candidates = []
    for email in received:
        conv_id = email.get("conversation_id")
        sender = (email.get("sender_email") or "").lower()
        contact = contacts.get(sender, {})
        contact_type = contact.get("contact_type", "unknown")

        # Find user's reply in this conversation (sent after this email)
        user_reply = _find_user_reply(
            sent_by_conv, conv_id, email.get("received_time"), alias_set,
            sent_by_subject=sent_by_subject, subject=email.get("subject"),
        )

        # Fetch thread emails for context
        thread_emails = []
        if conv_id:
            thread_emails = db.fetch_thread_emails(
                user_id, conv_id,
                before_time=None,
                limit=10,
            )

        thread_depth = len(thread_emails)
        email_signals = signals.get(email["id"], {})

        cal_email = CalibrationEmail(
            db_id=email["id"],
            incoming_email=email,
            thread_emails=thread_emails,
            user_reply=user_reply,
            contact_type=contact_type,
            thread_depth=thread_depth,
            signal_scores=email_signals,
        )

        # Skip emails the user never replied to — calibration only tests drafts
        if cal_email.user_reply is None:
            continue

        # Tag with all qualifying buckets
        cal_email.selection_buckets = _compute_buckets(cal_email)
        candidates.append(cal_email)

    reply_count = sum(1 for c in candidates if c.user_reply is not None)
    logger.info(f"[CAL-SEL] candidate pool: {len(candidates)} emails, {reply_count} with reply, {len(candidates) - reply_count} without")
    # Log bucket distribution
    from collections import Counter as _Counter
    bucket_dist = _Counter()
    for c in candidates:
        for b in c.selection_buckets:
            bucket_dist[b] += 1
    logger.debug(f"[CAL-SEL] bucket distribution in pool: {dict(bucket_dist)}")

    # Greedy allocation
    selected = _greedy_select(candidates)
    logger.info(f"Selected {len(selected)} calibration emails")
    return selected


def _fetch_received_emails(db, user_id, cutoff):
    """Fetch received emails after cutoff."""
    result = (
        db.client.table("emails")
        .select("id, subject, sender, sender_name, sender_email, body, "
                "received_time, conversation_id, to_field, cc_field, "
                "folder, has_attachments, recipients")
        .eq("user_id", user_id)
        .gte("received_time", cutoff)
        .neq("folder", "Sent Items")
        .order("received_time", desc=True)
        .limit(200)
        .execute()
    )
    return result.data or []


_SUBJECT_PREFIX_RE = re.compile(r"^(re|fw|fwd)\s*:\s*", re.IGNORECASE)


def _normalize_subject(subject):
    """Strip reply/forward prefixes and normalize for matching."""
    if not subject:
        return ""
    s = _SUBJECT_PREFIX_RE.sub("", subject)
    while _SUBJECT_PREFIX_RE.match(s):
        s = _SUBJECT_PREFIX_RE.sub("", s)
    return s.strip().lower()


def _fetch_sent_emails_by_conversation(db, user_id, alias_set):
    """Fetch sent emails grouped by conversation_id and normalized subject.

    Returns:
        tuple: (by_conv dict, by_subject dict)
    """
    result = (
        db.client.table("emails")
        .select("id, conversation_id, sender_email, body, received_time, subject")
        .eq("user_id", user_id)
        .eq("folder", "Sent Items")
        .order("received_time", desc=False)
        .limit(500)
        .execute()
    )
    by_conv = {}
    by_subject = {}
    for row in (result.data or []):
        conv_id = row.get("conversation_id")
        if conv_id:
            by_conv.setdefault(conv_id, []).append(row)
        norm_subj = _normalize_subject(row.get("subject"))
        if norm_subj:
            by_subject.setdefault(norm_subj, []).append(row)
    return by_conv, by_subject


def _find_user_reply(sent_by_conv, conversation_id, received_time, alias_set,
                     sent_by_subject=None, subject=None):
    """Find the user's reply to a specific email in a conversation.

    Primary lookup: conversation_id match.
    Fallback: normalized subject match within 48-hour window.
    """
    if not received_time:
        return None

    # Primary: conversation_id lookup
    if conversation_id:
        sent_in_conv = sent_by_conv.get(conversation_id, [])
        for sent in sent_in_conv:
            sent_time = sent.get("received_time", "")
            if sent_time > received_time:
                body = sent.get("body", "")
                if body and len(body.strip()) > 5:
                    return body

    # Fallback: subject-based lookup within 48-hour window
    if sent_by_subject and subject:
        norm_subj = _normalize_subject(subject)
        if norm_subj:
            candidates = sent_by_subject.get(norm_subj, [])
            for sent in candidates:
                sent_time = sent.get("received_time", "")
                if not sent_time or sent_time <= received_time:
                    continue
                # Check within 48-hour window
                try:
                    recv_dt = datetime.fromisoformat(received_time.replace("Z", "+00:00"))
                    sent_dt = datetime.fromisoformat(sent_time.replace("Z", "+00:00"))
                    if (sent_dt - recv_dt).total_seconds() > 48 * 3600:
                        continue
                except (ValueError, TypeError):
                    continue
                body = sent.get("body", "")
                if body and len(body.strip()) > 5:
                    return body

    return None


def _fetch_signal_scores(db, user_id, email_ids):
    """Fetch signal extraction results from response_events."""
    if not email_ids:
        return {}
    # Batch in chunks
    signals = {}
    for i in range(0, len(email_ids), 100):
        chunk = email_ids[i:i + 100]
        result = (
            db.client.table("response_events")
            .select("email_id, mc, ar, ub, dl, rt, pri, draft, reason")
            .eq("user_id", user_id)
            .in_("email_id", chunk)
            .execute()
        )
        for row in (result.data or []):
            signals[row["email_id"]] = row
    return signals


def _compute_buckets(cal_email):
    """Determine which stratification buckets an email qualifies for."""
    buckets = []

    # Contact type buckets
    ct = (cal_email.contact_type or "").lower()
    for bucket, types in _CONTACT_TYPE_MAP.items():
        if ct in types:
            buckets.append(bucket)
            break
    else:
        buckets.append("external_casual")  # default bucket

    # Thread complexity buckets
    depth = cal_email.thread_depth
    if depth <= 1:
        buckets.append("standalone")
    elif depth <= 4:
        buckets.append("mid_thread")
    else:
        buckets.append("complex_thread")

    # Signal buckets
    signals = cal_email.signal_scores
    if signals.get("mc"):
        buckets.append("material_consequence")
    if signals.get("ar"):
        buckets.append("action_request")
    if signals.get("ub"):
        buckets.append("user_bottleneck")

    return buckets


def _greedy_select(candidates):
    """Greedy stratified selection: pick emails that fill underrepresented buckets."""
    # Track how many we've selected per bucket
    bucket_counts = {b: 0 for b in BUCKET_TARGETS}
    selected = []
    used_ids = set()

    # Sort by recency (newest first)
    candidates.sort(key=lambda c: c.incoming_email.get("received_time", ""), reverse=True)

    # Pass 1: greedily fill buckets
    for candidate in candidates:
        if len(selected) >= TARGET_COUNT:
            break
        if candidate.db_id in used_ids:
            continue

        # Score: how many underrepresented buckets does this fill?
        need_score = 0
        for bucket in candidate.selection_buckets:
            if bucket in bucket_counts:
                target = BUCKET_TARGETS.get(bucket, 0)
                if bucket_counts[bucket] < target:
                    need_score += 1

        if need_score > 0:
            selected.append(candidate)
            used_ids.add(candidate.db_id)
            for bucket in candidate.selection_buckets:
                if bucket in bucket_counts:
                    bucket_counts[bucket] += 1

    # Pass 2: fill remaining slots with any candidate
    if len(selected) < TARGET_COUNT:
        for candidate in candidates:
            if len(selected) >= TARGET_COUNT:
                break
            if candidate.db_id not in used_ids:
                selected.append(candidate)
                used_ids.add(candidate.db_id)

    return selected
