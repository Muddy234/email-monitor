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

    # Short emails: signature and content are inseparable, don't strip
    if len(body) < 200:
        return body

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
# Content-aware body isolation (diff against prior thread emails)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r'\s+')
_QUOTE_PREFIX_RE = re.compile(r'^>+\s?', re.MULTILINE)

# Minimum anchor length to avoid false substring matches
_MIN_ANCHOR_LEN = 60

# Anchor window offsets — tries multiple starting positions to avoid
# false matches when prior bodies share common greetings (e.g. "Hi Nate,").
_ANCHOR_OFFSETS = (0, 100, 200)


def _normalize_for_match(text):
    """Collapse whitespace and strip '>' quote prefixes for fuzzy matching."""
    text = _QUOTE_PREFIX_RE.sub('', text)
    return _WHITESPACE_RE.sub(' ', text).strip()


def isolate_new_content(raw_body, prior_bodies):
    """Extract only the new message by diffing against prior email bodies.

    For each prior body (longest first), tries multiple anchor windows
    from its opening text. If found, returns everything before the match
    position. Falls back to strip_reply_markers() when no prior body
    matches.
    """
    if not raw_body:
        return ""
    if not prior_bodies:
        return strip_reply_markers(raw_body)

    norm_body = _normalize_for_match(raw_body)

    # Try each prior body, longest first — longest match is most specific
    sorted_priors = sorted(prior_bodies, key=len, reverse=True)

    for prior in sorted_priors:
        if not prior:
            continue

        norm_prior = _normalize_for_match(prior)
        if len(norm_prior) < _MIN_ANCHOR_LEN:
            continue

        # Try multiple anchor windows — common greetings in the first
        # 200 chars can cause false matches in threads with repeated
        # participants. Offset anchors find unique match points.
        for anchor_start in _ANCHOR_OFFSETS:
            if len(norm_prior) < anchor_start + _MIN_ANCHOR_LEN:
                break
            anchor = norm_prior[anchor_start:anchor_start + 200]

            pos = norm_body.find(anchor)
            if pos < 0:
                continue
            if pos == 0:
                # Entire body is quoted content — no new message
                return ""

            raw_pos = _map_norm_pos_to_raw(raw_body, pos)
            extracted = raw_body[:raw_pos].rstrip()

            if len(extracted) < 20:
                continue

            # Debug: check for position-mapping drift — if the tail of
            # extracted text overlaps with the prior body's anchor, the
            # cut point was too late and includes quoted content.
            if logger.isEnabledFor(logging.DEBUG):
                norm_extracted = _normalize_for_match(extracted)
                check = norm_prior[:100]
                if len(check) >= _MIN_ANCHOR_LEN and check in norm_extracted[-200:]:
                    logger.warning(
                        "isolate_new_content: possible position-mapping drift — "
                        "extracted text tail overlaps with prior body anchor"
                    )

            logger.debug(
                f"isolate_new_content: matched prior anchor "
                f"(offset={anchor_start}) at raw pos {raw_pos}"
            )
            return extracted

    # No prior body matched — fall back to regex stripping
    return strip_reply_markers(raw_body)


def _map_norm_pos_to_raw(raw_text, norm_pos):
    """Map a character position in normalized text back to the raw text.

    Walks through the raw text character by character, counting how many
    normalized characters have been consumed (collapsing runs of whitespace
    into a single space and skipping '>' quote prefixes at line starts).
    Returns the raw position when norm_pos normalized chars have been seen.
    """
    norm_count = 0
    i = 0
    in_whitespace = False
    at_line_start = True

    while i < len(raw_text) and norm_count < norm_pos:
        ch = raw_text[i]

        # Skip '>' quote prefixes at line starts
        if at_line_start and ch == '>':
            i += 1
            # Skip optional space after '>'
            if i < len(raw_text) and raw_text[i] == ' ':
                i += 1
            continue

        if ch in (' ', '\t', '\n', '\r'):
            if not in_whitespace:
                norm_count += 1  # collapsed whitespace = 1 space
                in_whitespace = True
            i += 1
            at_line_start = ch in ('\n', '\r')
        else:
            norm_count += 1
            in_whitespace = False
            at_line_start = False
            i += 1

    return i


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

def compute_thread_meta(thread_row, sender_email, user_aliases,
                        thread_emails=None):
    """Compute thread depth and unanswered flag for the context line.

    Args:
        thread_row: dict from threads table (or None).
        sender_email: Current email's sender (lowercase).
        user_aliases: list[str] of user email addresses (lowercase).
        thread_emails: Optional list of prior email dicts (ordered by
            received_time desc). When available, used for more accurate
            has_unanswered detection by checking the last message sender.

    Returns:
        tuple: (depth: int, has_unanswered: bool)
    """
    if not thread_row:
        return 1, False

    depth = thread_row.get("total_messages", 1)

    # has_unanswered: is the most recent prior message from someone other
    # than the user? If so, the user hasn't replied yet.
    if thread_emails:
        last_sender = (thread_emails[0].get("sender") or "").lower()
        has_unanswered = last_sender not in user_aliases
    else:
        # Fallback: no thread emails available, use aggregate counts
        user_msgs = thread_row.get("user_messages", 0)
        has_unanswered = depth > 1 and user_msgs == 0

    return depth, has_unanswered


# ---------------------------------------------------------------------------
# Full pre-processing pipeline
# ---------------------------------------------------------------------------

def pre_process_email(email_data, prior_bodies=None):
    """Run the full pre-processing pipeline on an email body.

    Args:
        email_data: dict with 'body' and 'subject' keys.
        prior_bodies: Optional list of prior email body strings from the
            same thread. When provided, uses content-aware diffing instead
            of regex-based reply stripping.

    Returns:
        str: Cleaned, truncated email body ready for Haiku.
    """
    body = email_data.get("body") or ""
    if prior_bodies:
        body = isolate_new_content(body, prior_bodies)
    else:
        body = strip_reply_markers(body)
    body = strip_signatures(body)
    body = truncate_smart(body, max_tokens=1000)
    return body
