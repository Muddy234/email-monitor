"""Pre-processing for signal extraction — reply stripping, signature
stripping, truncation, and sender tier resolution.

Runs before the Haiku signal extraction call. Reduces token count
and assembles the sender context line.
"""

import re
import logging

logger = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# Reply-marker stripping
# ---------------------------------------------------------------------------

_REPLY_MARKERS = [
    re.compile(r'^-{3,}\s*Original Message\s*-{3,}', re.MULTILINE | re.IGNORECASE),
    re.compile(r'^From:\s+.+@.+', re.MULTILINE),
    re.compile(r'^On\s+.+wrote:\s*$', re.MULTILINE),
    re.compile(r'^_{10,}', re.MULTILINE),
    re.compile(r'^\>{3,}', re.MULTILINE),
    re.compile(r'^Sent from my ', re.MULTILINE | re.IGNORECASE),
    re.compile(r'^Get Outlook for ', re.MULTILINE | re.IGNORECASE),
]


def strip_reply_markers(body):
    """Remove everything below the first reply boundary."""
    if not body:
        return ""

    earliest_pos = len(body)
    for pattern in _REPLY_MARKERS:
        match = pattern.search(body)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()

    return body[:earliest_pos].rstrip()


# ---------------------------------------------------------------------------
# Signature stripping
# ---------------------------------------------------------------------------

_SIGNATURE_PATTERNS = [
    # Common sign-off lines followed by name
    re.compile(
        r'\n\s*(?:Best regards|Kind regards|Regards|Thanks|Thank you|Sincerely|Cheers|Best|Warm regards|Respectfully|V/r)'
        r'\s*[,.]?\s*\n',
        re.IGNORECASE,
    ),
    # Dashes or underscores separating signature
    re.compile(r'\n\s*[-_]{2,}\s*\n'),
    # Confidentiality disclaimer blocks
    re.compile(
        r'\n\s*(?:CONFIDENTIAL|DISCLAIMER|This (?:email|message) (?:is|and any) )',
        re.IGNORECASE,
    ),
]


def strip_signatures(body):
    """Remove common email signature patterns from the end of the body."""
    if not body:
        return ""

    # Only search in the last 40% of the body to avoid false positives
    search_start = max(0, int(len(body) * 0.6))
    tail = body[search_start:]

    earliest_pos = len(tail)
    for pattern in _SIGNATURE_PATTERNS:
        match = pattern.search(tail)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()

    if earliest_pos < len(tail):
        return body[:search_start + earliest_pos].rstrip()
    return body


# ---------------------------------------------------------------------------
# Smart truncation
# ---------------------------------------------------------------------------

def truncate_smart(body, max_tokens=1000):
    """Truncate body to approximate token limit.

    Uses ~4 chars per token heuristic. If over limit, takes
    first 400 tokens + last 400 tokens with a truncation marker.
    """
    if not body:
        return ""

    chars_per_token = 4
    max_chars = max_tokens * chars_per_token

    if len(body) <= max_chars:
        return body

    # First 400 tokens + last 400 tokens
    head_chars = 400 * chars_per_token  # 1600
    tail_chars = 400 * chars_per_token  # 1600

    head = body[:head_chars]
    tail = body[-tail_chars:]
    return f"{head}\n\n[... truncated ...]\n\n{tail}"


# ---------------------------------------------------------------------------
# Sender tier resolution
# ---------------------------------------------------------------------------

def resolve_sender_tier(sender_email, contact, user_domain, domain_tiers_cache):
    """Resolve the sender's tier using the waterfall:
    1. sender_tier_override on contact (if set)
    2. domain_tiers table lookup
    3. Org domain match → 'I'
    4. Prior email history → 'P'
    5. Fallback → 'U'

    Args:
        sender_email: Sender's email address (lowercase).
        contact: Contact row dict (or None).
        user_domain: The user's org domain (e.g. 'arete-collective.com').
        domain_tiers_cache: dict mapping domain → tier letter.

    Returns:
        str: One of 'C', 'I', 'P', 'U'.
    """
    # 1. Manual override on contact
    if contact and contact.get("sender_tier_override"):
        return contact["sender_tier_override"]

    # Extract domain
    domain = sender_email.split("@")[1] if "@" in sender_email else ""

    # 2. Domain tiers table
    if domain and domain in domain_tiers_cache:
        return domain_tiers_cache[domain]

    # 3. Org domain match
    if domain and user_domain and domain == user_domain:
        return "I"

    # 4. Internal contact type
    if contact and contact.get("contact_type") == "internal":
        return "I"

    # 5. Prior history
    if contact and (contact.get("total_received") or 0) > 0:
        return "P"

    # 6. Fallback
    return "U"


# ---------------------------------------------------------------------------
# Thread metadata
# ---------------------------------------------------------------------------

def compute_thread_meta(thread_row, sender_email, user_aliases):
    """Compute thread depth and unanswered flag for the context line.

    Args:
        thread_row: dict from threads table (or None).
        sender_email: Current email's sender (lowercase).
        user_aliases: list[str] of user email addresses (lowercase).

    Returns:
        tuple: (depth: int, has_unanswered: bool)
    """
    if not thread_row:
        return 1, False

    depth = thread_row.get("total_messages", 1)

    # has_unanswered: sender has a prior message the user hasn't replied to
    # Approximation: user_messages == 0 and total > 1 means unanswered
    user_msgs = thread_row.get("user_messages", 0)
    has_unanswered = depth > 1 and user_msgs == 0

    return depth, has_unanswered


# ---------------------------------------------------------------------------
# Full pre-processing pipeline
# ---------------------------------------------------------------------------

def pre_process_email(email_data):
    """Run the full pre-processing pipeline on an email body.

    Args:
        email_data: dict with 'body' and 'subject' keys.

    Returns:
        str: Cleaned, truncated email body ready for Haiku.
    """
    body = email_data.get("body") or ""
    body = strip_reply_markers(body)
    body = strip_signatures(body)
    body = truncate_smart(body, max_tokens=1000)
    return body
