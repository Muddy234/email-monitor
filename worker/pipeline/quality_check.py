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

# Tightened pattern for stripping. Requires the line to end with a comma or
# exclamation mark (i.e., be structurally a greeting, not just any line that
# starts with "Hi"). Allows 0-3 trailing words to cover targets like
# "Hi Tyler,", "Hi all,", "Hi Tyler and Rebecca,", "Dear Mr. Smith,".
_GREETING_STRIP_RE = re.compile(
    r"^\s*(?:Hi|Hello|Hey|Dear|Good\s+(?:morning|afternoon|evening))"
    r"(?:[ \t]+[\w'\-]+){0,3}\s*[,!]\s*$",
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


_MAX_SIGNATURE_LINES = 4
_MAX_SIGNATURE_WORDS_PER_LINE = 5
_MAX_SIGNOFF_STRIP_CHARS = 200


def _looks_like_trailing_signoff(draft: str, match: re.Match) -> bool:
    """True if the sign-off match is structurally at the end of the draft.

    Accepts the match if everything after it is either empty or a short
    signature block (≤4 lines, ≤5 words each). Rejects when the phrase
    appears mid-body with substantive content following — for example a
    quoted previous email continuing into the user's actual reply.
    """
    tail = draft[match.end():].strip()
    if not tail:
        return True
    tail_lines = [l.strip() for l in tail.split("\n") if l.strip()]
    if len(tail_lines) > _MAX_SIGNATURE_LINES:
        return False
    return all(
        len(l.split()) <= _MAX_SIGNATURE_WORDS_PER_LINE for l in tail_lines
    )


def _strip_signoff(draft: str) -> tuple[list[str], list[str], str, bool]:
    """Strip the trailing sign-off block (closing phrase + signature lines).

    Always runs unconditionally. Eliminates the wrong-name-in-signoff bug
    class structurally — if no sign-off is stored, the LLM cannot sign as
    the wrong person.

    Guards against over-stripping:
    - Trailing-signoff guard: only strips when the matched phrase looks
      structurally like it ends the draft (short or empty tail).
    - Defensive cap: if the strip would remove >200 chars, skip and flag.

    Auto-fix flags emitted (in `auto_fixed`):
    - `stripped_signoff:<N>` — sign-off removed; <N> chars stripped
    - `signoff_strip_skipped_midbody` — match found mid-body; left alone
    - `signoff_strip_skipped_too_long` — strip would have removed too much

    Never sets needs_retry.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    """
    auto_fixed: list[str] = []
    fixed = draft

    matches = list(_SIGNOFF_RE.finditer(fixed))
    if not matches:
        return [], auto_fixed, fixed, False

    last_match = matches[-1]

    if not _looks_like_trailing_signoff(fixed, last_match):
        auto_fixed.append("signoff_strip_skipped_midbody")
        return [], auto_fixed, fixed, False

    # Walk back to the start of the line containing the sign-off phrase.
    line_start = fixed.rfind("\n", 0, last_match.start())
    if line_start == -1:
        line_start = 0
    else:
        line_start += 1  # skip past the newline itself

    strip_chars = len(fixed) - line_start
    if strip_chars > _MAX_SIGNOFF_STRIP_CHARS:
        auto_fixed.append("signoff_strip_skipped_too_long")
        return [], auto_fixed, fixed, False

    fixed = fixed[:line_start].rstrip()
    auto_fixed.append(f"stripped_signoff:{strip_chars}")
    return [], auto_fixed, fixed, False


def _strip_greeting(draft: str) -> tuple[list[str], list[str], str, bool]:
    """Strip the opening greeting line (and any blank lines after it).

    Always runs unconditionally. Uses a tightened regex
    (`_GREETING_STRIP_RE`) that requires the line to end with `,` or `!` —
    this prevents matching things like "Hi — quick thought on the
    contract" where "Hi" leads into body text.

    Auto-fix flag emitted:
    - `stripped_greeting:<N>` — greeting line removed; <N> chars stripped

    Never sets needs_retry.

    Returns (issues, auto_fixed, fixed_draft, needs_retry).
    """
    auto_fixed: list[str] = []
    lines = draft.split("\n")

    first_idx = -1
    for i, line in enumerate(lines):
        if line.strip():
            first_idx = i
            break

    if first_idx == -1:
        return [], [], draft, False

    if not _GREETING_STRIP_RE.match(lines[first_idx]):
        return [], [], draft, False

    # Calculate how many chars we're removing (the greeting line + any
    # blank lines that follow, before joining the remainder).
    new_lines = lines[first_idx + 1:]
    while new_lines and not new_lines[0].strip():
        new_lines.pop(0)
    fixed = "\n".join(new_lines).strip()
    strip_chars = len(draft) - len(fixed)

    auto_fixed.append(f"stripped_greeting:{strip_chars}")
    return [], auto_fixed, fixed, False


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
        lambda d: _strip_signoff(d),
        lambda d: _strip_greeting(d),
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
    """Build a revision note string from QC issues for the retry prompt.

    Sign-off and greeting issues are now stripped unconditionally in QC, so
    the remaining flag-only issues (unclosed_bracket, bare_placeholder,
    draft_too_short, draft_too_long) do not produce retry guidance.
    """
    return ""
