"""Phase 1: Collect and pre-filter emails for onboarding.

Queries 30 days of received + sent emails from Supabase, then filters
out noise (automated senders, calendar replies, empty bodies).
"""

import logging
import re

logger = logging.getLogger("worker.onboarding")

# Reuse the same blacklists as the runtime pipeline (run_pipeline.py)
BLACKLIST_SENDERS = [
    "noreply@",
    "no-reply@",
    "mailer-daemon@",
    "postmaster@",
    "notifications@github.com",
    "builds@travis-ci.org",
    "jira@",
    "calendar-notification@google.com",
    "notify@",
    "donotreply@",
    "automated@",
    "newsletter@",
    "marketing@",
    "updates@",
]

BLACKLIST_SUBJECT_PATTERNS = [
    "accepted:",
    "declined:",
    "tentatively accepted:",
    "canceled:",
    "undeliverable:",
    "out of office",
    "automatic reply",
    "auto-reply",
    "delivery status notification",
    "read receipt:",
    "unsubscribe confirmation",
]

# Regex to strip quoted replies (lines starting with > or "On ... wrote:")
_QUOTED_REPLY_RE = re.compile(
    r"(?:^>.*$|^On .+ wrote:$)", re.MULTILINE | re.IGNORECASE
)
# Common signature markers
_SIGNATURE_RE = re.compile(
    r"(?:^--|^___+|^Sent from my |^Get Outlook for )", re.MULTILINE | re.IGNORECASE
)
# Legal disclaimer blocks — require disclaimer-specific continuation to avoid
# matching body content like "This email and the attached documents reflect..."
_DISCLAIMER_RE = re.compile(
    r"(?:^CONFIDENTIALITY|^DISCLAIMER|^This email and any .+(?:confidential|intended|privilege))",
    re.MULTILINE | re.IGNORECASE,
)


def collect_onboarding_emails(db, user_id, user_aliases, days=120, max_emails=None):
    """Fetch and split emails into received vs sent.

    Args:
        db: SupabaseWorkerClient instance.
        user_id: UUID string.
        user_aliases: List of user's email addresses.
        days: Lookback window.
        max_emails: Optional cap on total emails fetched.

    Returns:
        dict with keys: received, sent, filtered_count, total_count.
    """
    all_emails = db.fetch_emails_for_onboarding(user_id, days=days, max_emails=max_emails)
    total = len(all_emails)
    logger.info(f"Fetched {total} emails for user {user_id} ({days}-day window)")

    aliases_lower = {a.lower() for a in user_aliases} if user_aliases else set()

    received = []
    sent = []

    for email in all_emails:
        folder = (email.get("folder") or "").lower()
        sender_email = (email.get("sender_email") or "").lower()

        if folder == "sent items" or sender_email in aliases_lower:
            sent.append(email)
        else:
            received.append(email)

    # Filter empty-body sent emails before they enter sampling
    sent_before = len(sent)
    sent = [e for e in sent if len((e.get("body") or "").strip()) >= 10]
    sent_filtered = sent_before - len(sent)

    # Pre-filter noise from received emails
    received, filtered_count = pre_filter_emails(received)
    filtered_count += sent_filtered

    logger.info(
        f"Split: {len(received)} received, {len(sent)} sent, "
        f"{filtered_count} filtered out"
    )

    return {
        "received": received,
        "sent": sent,
        "filtered_count": filtered_count,
        "total_count": total,
    }


def pre_filter_emails(emails):
    """Remove automated/noise emails.

    Returns:
        tuple: (filtered_list, removed_count)
    """
    kept = []
    removed = 0

    for email in emails:
        sender = (email.get("sender_email") or email.get("sender") or "").lower()
        subject = (email.get("subject") or "").lower()
        body = email.get("body") or ""

        # Blacklist senders
        matched_bl = next((bl for bl in BLACKLIST_SENDERS if bl in sender), None)
        if matched_bl:
            logger.debug(f"Filtered sender '{sender}' — matched '{matched_bl}' | subject: {subject[:80]}")
            removed += 1
            continue

        # Blacklist subjects
        matched_subj = next((pat for pat in BLACKLIST_SUBJECT_PATTERNS if pat in subject), None)
        if matched_subj:
            logger.debug(f"Filtered subject '{subject[:80]}' — matched '{matched_subj}'")
            removed += 1
            continue

        # Empty body
        if len(body.strip()) < 10:
            logger.debug(f"Filtered empty body | subject: {subject[:80]}")
            removed += 1
            continue

        # Calendar invites (iCalendar content)
        if "BEGIN:VCALENDAR" in body:
            logger.debug(f"Filtered calendar invite | subject: {subject[:80]}")
            removed += 1
            continue

        kept.append(email)

    return kept, removed


def clean_email_body(body, max_chars=1500):
    """Strip signatures, disclaimers, and quoted replies, then truncate.

    Args:
        body: Raw email body text.
        max_chars: Maximum characters to return.

    Returns:
        Cleaned body string.
    """
    if not body:
        return ""

    # Search for signature/disclaimer only in the last 40% of the body
    # to avoid false positives on body content (e.g., "Sent from my
    # perspective, this deal looks strong").
    tail_start = max(0, int(len(body) * 0.6))

    # Find and truncate at signature marker (tail only)
    sig_match = _SIGNATURE_RE.search(body, pos=tail_start)
    if sig_match:
        body = body[:sig_match.start()]

    # Find and truncate at disclaimer (tail only)
    disc_match = _DISCLAIMER_RE.search(body, pos=tail_start)
    if disc_match:
        body = body[:disc_match.start()]

    # Remove quoted reply lines
    body = _QUOTED_REPLY_RE.sub("", body)

    # Collapse multiple blank lines
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # Truncate
    if len(body) > max_chars:
        body = body[:max_chars] + "..."

    return body
