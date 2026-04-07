"""Layer 1: Score the user's actual sent emails against profile dimensions."""

import logging
import re
from dataclasses import dataclass, asdict

from pipeline.api_client import call_claude, resolve_model
from calibration.prompts import BEHAVIORAL_SCORING_PROMPT, PREFERENCE_SCORING_PROMPT

logger = logging.getLogger("worker.calibration")

MODEL = "opus"


@dataclass
class StyleScore:
    word_count: int
    sentence_count: int
    greeting_type: str       # none | minimal | professional | warm
    signoff_type: str        # none | signature_block | simple_closing | warm_closing
    uses_contractions: bool
    uses_bullets: bool
    exclamation_count: int
    question_count: int


@dataclass
class BehavioralScore:
    decisiveness: str        # decides | proposes_solution | defers | delegates | no_signal
    thoroughness: str        # addresses_all | key_point_only | no_signal
    specificity: str         # specific_next_step | conditional_decision | vague_forward | no_signal


@dataclass
class PreferenceScore:
    investment_signal: str   # active | selective | conservative | no_signal
    positional_signal: str   # advancing | measured | yielding | no_signal


@dataclass
class ContextualGroundTruth:
    user_replied: bool
    reply_word_count: int | None
    user_is_primary_recipient: bool
    thread_depth: int


@dataclass
class GroundTruthScore:
    email_id: str
    style: StyleScore
    behavioral: BehavioralScore
    preference: PreferenceScore
    context: ContextualGroundTruth


def score_ground_truth(cal_email, api_key=None):
    """Score a single calibration email's actual reply across all dimensions.

    Returns:
        GroundTruthScore or None if the email has no reply and only
        contextual scoring applies.
    """
    reply = cal_email.user_reply
    incoming = cal_email.incoming_email
    incoming_body = incoming.get("body", "") or ""
    logger.debug(
        f"[GT] scoring {cal_email.db_id[:8]}: has_reply={reply is not None}, "
        f"incoming_len={len(incoming_body)}, reply_len={len(reply) if reply else 0}"
    )

    # Style scoring (mechanical)
    style = _score_style(reply) if reply else StyleScore(
        word_count=0, sentence_count=0, greeting_type="none",
        signoff_type="none", uses_contractions=False, uses_bullets=False,
        exclamation_count=0, question_count=0,
    )

    # Behavioral scoring (Opus)
    if reply:
        behavioral = _score_behavioral(incoming_body, reply, api_key)
    else:
        behavioral = BehavioralScore(
            decisiveness="no_signal", thoroughness="no_signal",
            specificity="no_signal",
        )

    # Preference scoring (Opus)
    if reply:
        thread_summary = _build_thread_context(cal_email.thread_emails)
        preference = _score_preference(incoming_body, reply, thread_summary, api_key)
    else:
        preference = PreferenceScore(
            investment_signal="no_signal", positional_signal="no_signal",
        )

    # Contextual (metadata)
    context = ContextualGroundTruth(
        user_replied=reply is not None,
        reply_word_count=len(reply.split()) if reply else None,
        user_is_primary_recipient=True,
        thread_depth=cal_email.thread_depth,
    )

    logger.debug(
        f"[GT] {cal_email.db_id[:8]} results: "
        f"style=({style.greeting_type}/{style.signoff_type}/{style.word_count}w), "
        f"behavioral=({behavioral.decisiveness}/{behavioral.thoroughness}/{behavioral.specificity}), "
        f"preference=({preference.investment_signal}/{preference.positional_signal})"
    )
    return GroundTruthScore(
        email_id=cal_email.db_id,
        style=style,
        behavioral=behavioral,
        preference=preference,
        context=context,
    )


# ---------------------------------------------------------------------------
# Style scoring (mechanical — no LLM)
# ---------------------------------------------------------------------------

_GREETING_PATTERNS = {
    "warm": re.compile(
        r"^(thanks\s+\w+[!,]|thank\s+you\s+\w+[!,]|hey\s+\w+\s*!)",
        re.IGNORECASE,
    ),
    "professional": re.compile(
        r"^(hi\s+\w+|hello\s+\w+|good\s+(morning|afternoon|evening)\s*\w*)[,\s–-]",
        re.IGNORECASE,
    ),
    "minimal": re.compile(
        r"^(hey\s+\w+|hey\s+guys|hey\s+all|hey\s+team)[,\s]",
        re.IGNORECASE,
    ),
}

_SIGNOFF_PATTERNS = {
    "warm_closing": re.compile(
        r"(warmly|warm\s+regards|all\s+the\s+best|take\s+care|cheers\s*!)\s*[,.]?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "simple_closing": re.compile(
        r"(best|thanks|thank\s+you|regards|best\s+regards|sincerely)\s*[,!.]?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "signature_block": re.compile(
        r"(--|___|\n[A-Z][a-z]+\s[A-Z][a-z]+\n.*(LLC|Inc|LP|Group|Capital))",
        re.MULTILINE,
    ),
}


def _score_style(text):
    """Mechanically score style dimensions of a reply."""
    if not text:
        return StyleScore(0, 0, "none", "none", False, False, 0, 0)

    words = text.split()
    word_count = len(words)
    sentences = re.split(r'[.!?]+', text)
    sentence_count = len([s for s in sentences if s.strip()])

    # Greeting
    first_line = text.strip().split("\n")[0] if text.strip() else ""
    greeting = "none"
    for gtype, pattern in _GREETING_PATTERNS.items():
        if pattern.match(first_line):
            greeting = gtype
            break

    # Sign-off (check last 40% of text)
    cutoff = max(len(text) // 2, len(text) - 500)
    tail = text[cutoff:]
    signoff = "none"
    for stype in ["warm_closing", "simple_closing", "signature_block"]:
        if _SIGNOFF_PATTERNS[stype].search(tail):
            signoff = stype
            break

    uses_contractions = bool(re.search(
        r"\b(I'm|I'll|I'd|we're|we'll|we'd|don't|doesn't|didn't|"
        r"can't|won't|wouldn't|shouldn't|couldn't|isn't|aren't|"
        r"it's|that's|there's|they're|you're|you'll|he's|she's)\b",
        text, re.IGNORECASE,
    ))
    uses_bullets = bool(re.search(r"^\s*[-•*]\s+", text, re.MULTILINE))

    return StyleScore(
        word_count=word_count,
        sentence_count=sentence_count,
        greeting_type=greeting,
        signoff_type=signoff,
        uses_contractions=uses_contractions,
        uses_bullets=uses_bullets,
        exclamation_count=text.count("!"),
        question_count=text.count("?"),
    )


# ---------------------------------------------------------------------------
# Behavioral scoring (Opus)
# ---------------------------------------------------------------------------

_BEHAVIORAL_VALUES = {
    "decisiveness": {"decides", "proposes_solution", "defers", "delegates", "no_signal"},
    "thoroughness": {"addresses_all", "key_point_only", "no_signal"},
    "specificity": {"specific_next_step", "conditional_decision", "vague_forward", "no_signal"},
}


def _score_behavioral(incoming_body, sent_body, api_key=None):
    """Use Opus to classify behavioral dimensions."""
    try:
        prompt = BEHAVIORAL_SCORING_PROMPT.format(
            incoming_email_body=incoming_body[:3000],
            sent_email_body=sent_body[:3000],
        )
        text, _ = call_claude(
            prompt=prompt,
            model=resolve_model(MODEL),
            max_tokens=50,
            temperature=0,
            api_key=api_key,
        )
        return _parse_behavioral(text)
    except Exception as e:
        logger.warning(f"Behavioral scoring failed: {e}")
        return BehavioralScore("no_signal", "no_signal", "no_signal")


def _parse_behavioral(text):
    """Parse the structured behavioral scoring response."""
    result = {"decisiveness": "no_signal", "thoroughness": "no_signal", "specificity": "no_signal"}
    for line in text.strip().splitlines():
        line = line.strip()
        for dim in result:
            prefix = f"{dim.upper()}:"
            if line.upper().startswith(prefix):
                value = line[len(prefix):].strip().lower()
                if value in _BEHAVIORAL_VALUES[dim]:
                    result[dim] = value
    return BehavioralScore(**result)


# ---------------------------------------------------------------------------
# Preference scoring (Opus)
# ---------------------------------------------------------------------------

_PREFERENCE_VALUES = {
    "investment_signal": {"active", "selective", "conservative", "no_signal"},
    "positional_signal": {"advancing", "measured", "yielding", "no_signal"},
}


def _score_preference(incoming_body, sent_body, thread_summary, api_key=None):
    """Use Opus to classify preference dimensions."""
    try:
        prompt = PREFERENCE_SCORING_PROMPT.format(
            incoming_email_body=incoming_body[:3000],
            sent_email_body=sent_body[:3000],
            thread_summary_or_none=thread_summary or "None.",
        )
        text, _ = call_claude(
            prompt=prompt,
            model=resolve_model(MODEL),
            max_tokens=30,
            temperature=0,
            api_key=api_key,
        )
        return _parse_preference(text)
    except Exception as e:
        logger.warning(f"Preference scoring failed: {e}")
        return PreferenceScore("no_signal", "no_signal")


def _parse_preference(text):
    """Parse the structured preference scoring response."""
    result = {"investment_signal": "no_signal", "positional_signal": "no_signal"}
    for line in text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("INVESTMENT:"):
            value = line[len("INVESTMENT:"):].strip().lower()
            if value in _PREFERENCE_VALUES["investment_signal"]:
                result["investment_signal"] = value
        elif line.upper().startswith("POSITIONAL:"):
            value = line[len("POSITIONAL:"):].strip().lower()
            if value in _PREFERENCE_VALUES["positional_signal"]:
                result["positional_signal"] = value
    return PreferenceScore(**result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_thread_context(thread_emails):
    """Build a simple thread context string from thread emails."""
    if not thread_emails:
        return "None."
    lines = []
    for te in thread_emails[:5]:
        sender = te.get("sender_name") or te.get("sender", "Unknown")
        snippet = (te.get("body") or "")[:200]
        lines.append(f"{sender}: {snippet}")
    return "\n".join(lines) if lines else "None."


def ground_truth_to_dict(gt):
    """Convert a GroundTruthScore to a JSON-serializable dict."""
    return asdict(gt)
