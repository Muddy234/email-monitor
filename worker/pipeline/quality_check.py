"""Rule-based quality check for generated email drafts.

Runs after _validate_output and before insert_draft. Catches structural
issues the LLM sometimes produces (truncated sign-offs, leaked thinking
artifacts, duplicate tags) without second-guessing the response logic.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("worker")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class QCConfig:
    """Configuration for quality checks, parsed from style guide."""
    user_name: str
    recipient_name: str = ""
    multi_recipient: bool = False
    greetings_expected: bool = True
    signoff_expected: bool = True
    target_word_range: tuple[int, int] | None = None


@dataclass
class QCResult:
    """Result of a quality check run."""
    passed: bool                        # False = needs retry
    issues: list[str] = field(default_factory=list)       # Un-fixed (for DB)
    auto_fixed: list[str] = field(default_factory=list)   # Fixed (for logging)
    draft: str = ""


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_SIGNOFF_RE = re.compile(
    r'(?:Best regards|Kind regards|Warm regards|Regards|Thanks|Thank you|'
    r'Sincerely|Cheers|Best|Respectfully|V/r)\s*[,.]?\s*$',
    re.IGNORECASE | re.MULTILINE,
)

_GREETING_RE = re.compile(
    r'^\s*(?:Hi|Hello|Hey|Dear|Good\s+(?:morning|afternoon|evening))\b',
    re.IGNORECASE,
)

_USER_CONFIRM_RE = re.compile(r'\[USER TO CONFIRM(?::\s*([^\]]*))?\]')

_STANDALONE_CONFIRM_RE = re.compile(
    r'^\s*\[USER TO CONFIRM[^\]]*\]\s*$', re.MULTILINE,
)

_BARE_PLACEHOLDER_RE = re.compile(r'\[PLACEHOLDER\]', re.IGNORECASE)

_THINKING_FRAGMENT_RE = re.compile(r'</?thinking>', re.IGNORECASE)

# Exact allowlist — must be on its own line (followed by newline).
_META_COMMENTARY_RE = re.compile(
    r"^(?:Here's a draft|Here is a draft|Here's my response|Here is my response|"
    r"Here's the draft|Here is the draft|"
    r"Sure,? here's|Sure,? here is|"
    r"I'll draft|Let me draft|Here you go)[^\n]*\n+",
    re.IGNORECASE,
)

_HEADER_LEAK_RE = re.compile(
    r'^(?:Subject|Re|To|From|CC|BCC)\s*:.*\n',
    re.IGNORECASE,
)

# Style guide parsing patterns
_PLEASANTRY_RE = re.compile(r'-\s*Pleasantries:\s*(.*)', re.IGNORECASE)
_GREETING_PATTERN_RE = re.compile(r'-\s*Greeting pattern:\s*(.*)', re.IGNORECASE)
_SIGNOFF_PATTERN_RE = re.compile(r'-\s*Sign-off pattern:\s*(.*)', re.IGNORECASE)
_WORD_RANGE_RE = re.compile(r'(\d+)\s*[-–]\s*(\d+)\s*words', re.IGNORECASE)
_RESPONSE_LENGTH_RE = re.compile(r'-\s*Response length:\s*(.*)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Style guide parsing
# ---------------------------------------------------------------------------

def parse_qc_config(
    user_name: str,
    recipient_name: str,
    style_guide: str,
    recipient_count: int = 1,
) -> QCConfig:
    """Extract QC-relevant settings from the style guide.

    Best-effort parsing with safe defaults. If the style guide can't be
    parsed, we assume greetings expected, sign-off expected, no length check.
    """
    config = QCConfig(
        user_name=user_name,
        recipient_name=recipient_name,
        multi_recipient=recipient_count > 1,
    )

    if not style_guide:
        return config

    # --- Greetings expected? ---
    # Check pleasantry level first
    m = _PLEASANTRY_RE.search(style_guide)
    if m and "minimal" in m.group(1).lower():
        config.greetings_expected = False

    # Check greeting pattern — if it's just "[First Name]," with no
    # Hi/Hello prefix, that's a no-greeting style
    m = _GREETING_PATTERN_RE.search(style_guide)
    if m:
        pattern_text = m.group(1).strip()
        has_greeting_word = bool(re.search(
            r'\b(?:Hi|Hello|Hey|Dear)\b', pattern_text, re.IGNORECASE,
        ))
        if not has_greeting_word:
            config.greetings_expected = False

    # --- Sign-off expected? ---
    m = _SIGNOFF_PATTERN_RE.search(style_guide)
    if m:
        pattern_text = m.group(1).strip().lower()
        if "none" in pattern_text or "no sign-off" in pattern_text:
            config.signoff_expected = False
    # If no sign-off pattern line at all, keep default True

    # --- Target word range ---
    m = _RESPONSE_LENGTH_RE.search(style_guide)
    if m:
        wm = _WORD_RANGE_RE.search(m.group(1))
        if wm:
            config.target_word_range = (int(wm.group(1)), int(wm.group(2)))

    return config


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_leaked_artifacts(draft: str) -> tuple[list[str], list[str], str, bool]:
    """Strip residual thinking tags, meta-commentary, and header leaks.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    """
    auto_fixed = []
    fixed = draft

    # Thinking fragments
    if _THINKING_FRAGMENT_RE.search(fixed):
        fixed = _THINKING_FRAGMENT_RE.sub("", fixed).strip()
        auto_fixed.append("stripped_thinking_fragment")

    # Meta-commentary prefix (only check start of draft)
    m = _META_COMMENTARY_RE.match(fixed)
    if m:
        fixed = fixed[m.end():].strip()
        auto_fixed.append("stripped_meta_commentary")

    # Header/subject leak (only first 3 lines)
    lines = fixed.split("\n", 3)
    stripped_count = 0
    while lines and stripped_count < 3:
        if _HEADER_LEAK_RE.match(lines[0] + "\n"):
            lines.pop(0)
            stripped_count += 1
        else:
            break
    if stripped_count:
        fixed = "\n".join(lines).strip()
        auto_fixed.append("stripped_header_leak")

    return [], auto_fixed, fixed, False


def _check_tag_hygiene(draft: str) -> tuple[list[str], list[str], str, bool]:
    """Check for tag issues: standalone confirms, duplicates, unclosed brackets.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    """
    issues = []
    auto_fixed = []
    fixed = draft

    # Standalone [USER TO CONFIRM] blocks before the greeting.
    # Find the first line that looks like actual email content (greeting or body).
    content_start = 0
    for i, line in enumerate(fixed.split("\n")):
        stripped = line.strip()
        if not stripped:
            continue
        if _STANDALONE_CONFIRM_RE.match(line):
            continue
        content_start = i
        break

    if content_start > 0:
        lines = fixed.split("\n")
        # Remove standalone confirm lines before content
        kept = []
        for i, line in enumerate(lines):
            if i < content_start and _STANDALONE_CONFIRM_RE.match(line):
                continue
            kept.append(line)
        new_draft = "\n".join(kept).strip()
        if new_draft != fixed:
            fixed = new_draft
            auto_fixed.append("stripped_standalone_user_to_confirm")

    # Duplicate [USER TO CONFIRM] with same description
    matches = _USER_CONFIRM_RE.findall(fixed)
    if matches:
        seen = set()
        for desc in matches:
            key = desc.strip().lower()
            if key in seen:
                # Remove the duplicate occurrence (keep first)
                pattern = re.compile(
                    r'\[USER TO CONFIRM:\s*' + re.escape(desc.strip()) + r'\]',
                    re.IGNORECASE,
                )
                all_matches = list(pattern.finditer(fixed))
                if len(all_matches) > 1:
                    # Remove from last to first to preserve indices
                    for m in reversed(all_matches[1:]):
                        fixed = fixed[:m.start()] + fixed[m.end():]
                    auto_fixed.append(f"deduplicated_user_to_confirm")
            seen.add(key)

    # Unclosed brackets (flag only)
    opens = fixed.count("[")
    closes = fixed.count("]")
    if opens != closes:
        issues.append("unclosed_bracket")

    # Bare [PLACEHOLDER] (flag only)
    if _BARE_PLACEHOLDER_RE.search(fixed):
        issues.append("bare_placeholder")

    return issues, auto_fixed, fixed, False


def _check_signoff(
    draft: str, user_name: str, signoff_expected: bool,
) -> tuple[list[str], list[str], str, bool]:
    """Check sign-off presence and name correctness.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    """
    if not signoff_expected:
        return [], [], draft, False

    issues = []
    auto_fixed = []
    fixed = draft
    needs_retry = False

    signoff_matches = list(_SIGNOFF_RE.finditer(fixed))

    if not signoff_matches:
        issues.append("missing_signoff")
        return issues, auto_fixed, fixed, True

    # Double sign-off: auto-fix, keep last
    if len(signoff_matches) > 1:
        last = signoff_matches[-1]
        first = signoff_matches[0]
        # Keep everything before first sign-off + everything from last sign-off
        fixed = fixed[:first.start()].rstrip() + "\n\n" + fixed[last.start():]
        auto_fixed.append("removed_duplicate_signoff")

    # Truncated user name: check the line after the last sign-off
    if user_name:
        # Re-find sign-off in (possibly fixed) draft
        matches = list(_SIGNOFF_RE.finditer(fixed))
        if matches:
            last_match = matches[-1]
            after_signoff = fixed[last_match.end():].strip()
            # The name should be on the first non-empty line after the sign-off
            name_line = ""
            for line in after_signoff.split("\n"):
                if line.strip():
                    name_line = line.strip()
                    break

            if not name_line:
                issues.append("missing_name_after_signoff")
                needs_retry = True
            elif name_line.lower() != user_name.lower():
                # Check for truncation (partial match at start)
                if (user_name.lower().startswith(name_line.lower())
                        and len(name_line) < len(user_name)):
                    issues.append("truncated_signoff_name")
                    needs_retry = True
                # If it's a completely different name, also flag
                elif user_name.split()[0].lower() not in name_line.lower():
                    issues.append("wrong_signoff_name")
                    needs_retry = True

    return issues, auto_fixed, fixed, needs_retry


def _check_greeting(
    draft: str, user_name: str, recipient_name: str,
    greetings_expected: bool, multi_recipient: bool,
) -> tuple[list[str], list[str], str, bool]:
    """Check greeting presence and correct addressing.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    """
    issues = []
    needs_retry = False

    # Get first non-empty line
    first_line = ""
    for line in draft.split("\n"):
        if line.strip():
            first_line = line.strip()
            break

    if not first_line:
        return issues, [], draft, False

    # Missing greeting
    if greetings_expected and not _GREETING_RE.match(first_line):
        issues.append("missing_greeting")
        needs_retry = True

    # Greeting addresses user instead of recipient
    # Skip if multi-recipient (greeting could be "Hi all," etc.)
    if not multi_recipient and user_name:
        user_first = user_name.split()[0].lower()
        recip_first = recipient_name.split()[0].lower() if recipient_name else ""

        # Only flag if the names are different
        if user_first != recip_first and user_first in first_line.lower():
            issues.append("greeting_addresses_user")
            needs_retry = True

    return issues, [], draft, needs_retry


def _check_length(
    draft: str, target_word_range: tuple[int, int] | None,
) -> tuple[list[str], list[str], str, bool]:
    """Flag drafts outside a generous band of the target word range.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    Never triggers retry — flag only.
    """
    if not target_word_range:
        return [], [], draft, False

    word_count = len(draft.split())
    lo, hi = target_word_range
    # Generous band: 0.25x min to 3x max
    band_lo = int(lo * 0.25)
    band_hi = int(hi * 3)

    issues = []
    if word_count < band_lo:
        issues.append(f"draft_too_short ({word_count} words, expected {lo}-{hi})")
    elif word_count > band_hi:
        issues.append(f"draft_too_long ({word_count} words, expected {lo}-{hi})")

    return issues, [], draft, False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_draft_quality(draft: str, config: QCConfig) -> QCResult:
    """Run all quality checks on a draft.

    Checks execute in order: leaked artifacts first (cleans junk so
    subsequent checks operate on cleaner text), then tag hygiene,
    sign-off, greeting, and length.
    """
    all_issues: list[str] = []
    all_auto_fixed: list[str] = []
    needs_retry = False
    current = draft

    checks = [
        lambda d: _check_leaked_artifacts(d),
        lambda d: _check_tag_hygiene(d),
        lambda d: _check_signoff(d, config.user_name, config.signoff_expected),
        lambda d: _check_greeting(
            d, config.user_name, config.recipient_name,
            config.greetings_expected, config.multi_recipient,
        ),
        lambda d: _check_length(d, config.target_word_range),
    ]

    for check_fn in checks:
        issues, auto_fixed, current, retry = check_fn(current)
        all_issues.extend(issues)
        all_auto_fixed.extend(auto_fixed)
        needs_retry = needs_retry or retry

    return QCResult(
        passed=not needs_retry,
        issues=all_issues,
        auto_fixed=all_auto_fixed,
        draft=current.strip(),
    )


def build_revision_notes(qc_result: QCResult, user_name: str) -> str:
    """Build a revision note string from QC issues for the retry prompt."""
    if not qc_result.issues:
        return ""

    notes = []
    for issue in qc_result.issues:
        if issue == "missing_signoff":
            notes.append(
                "The reply must end with a closing greeting "
                f"(e.g., Best regards,) followed by {user_name} on the next line."
            )
        elif issue in ("truncated_signoff_name", "wrong_signoff_name"):
            notes.append(
                f"The sign-off name was incorrect or truncated. "
                f"It must be exactly: {user_name}"
            )
        elif issue == "missing_name_after_signoff":
            notes.append(
                f"The sign-off greeting was present but the user's name was missing. "
                f"Add {user_name} on the line after the closing greeting."
            )
        elif issue == "missing_greeting":
            notes.append(
                "The reply is missing an opening greeting. "
                "Start with an appropriate greeting (e.g., Hi [Name],)."
            )
        elif issue == "greeting_addresses_user":
            notes.append(
                "The greeting addressed the author instead of the recipient. "
                "Address the greeting to the email's sender, not the user drafting the reply."
            )
        # Flag-only issues don't need revision notes

    if not notes:
        return ""

    return (
        "REVISION NOTE: The previous draft had these structural issues:\n"
        + "\n".join(f"- {n}" for n in notes)
        + "\nPlease ensure the reply corrects these problems."
    )
