"""Layer 2: Score generated drafts against Layer 1 ground truth."""

import logging
import re
from dataclasses import dataclass, asdict

from pipeline.api_client import call_claude, resolve_model
from calibration.prompts import (
    BEHAVIORAL_SCORING_PROMPT,
    PREFERENCE_SCORING_PROMPT,
    CONTEXTUAL_SCORING_PROMPT,
)
from calibration.ground_truth_scorer import (
    _score_style,
    _parse_behavioral,
    _parse_preference,
    BehavioralScore,
    PreferenceScore,
    GroundTruthScore,
)

logger = logging.getLogger("worker.calibration")

MODEL = "opus"

# Adjacency maps for ordinal dimensions
_GREETING_ORDER = ["none", "minimal", "professional", "warm"]
_SIGNOFF_ORDER = ["none", "signature_block", "simple_closing", "warm_closing"]
_DECISIVENESS_ORDER = ["decides", "proposes_solution", "defers", "delegates"]
_SPECIFICITY_ORDER = ["specific_next_step", "conditional_decision", "vague_forward"]


@dataclass
class StyleDelta:
    word_count_ratio: float
    greeting_match: str         # match | adjacent | hard_miss
    signoff_match: str          # match | adjacent | hard_miss
    contraction_match: str      # match | mismatch
    bullet_match: str           # match | mismatch
    formality_register: str     # match | adjacent | hard_miss


@dataclass
class BehavioralDelta:
    decisiveness_match: str     # match | adjacent | hard_miss
    thoroughness_match: str     # match | adjacent | hard_miss
    specificity_match: str      # match | adjacent | hard_miss


@dataclass
class PreferenceDelta:
    investment_match: str       # match | adjacent | hard_miss | not_applicable
    positional_match: str       # match | adjacent | hard_miss | not_applicable


@dataclass
class ContextualScore:
    should_draft_accuracy: str  # correct | incorrect
    content_alignment: str      # match | partial | hard_miss
    fabrication_detected: bool
    comprehension_pass: bool
    attribution_pass: bool


@dataclass
class CalibrationResult:
    email_id: str
    iteration: int
    ground_truth: GroundTruthScore
    generated_draft: str | None
    actual_reply: str | None
    incoming_body: str
    thread_summary: str
    style_delta: StyleDelta
    behavioral_delta: BehavioralDelta
    preference_delta: PreferenceDelta
    contextual: ContextualScore
    overall: str                # pass | soft_miss | hard_miss


def score_draft(cal_email, generated_draft, ground_truth, iteration,
                thread_summary, api_key=None):
    """Score a generated draft against the Layer 1 ground truth.

    Args:
        cal_email: CalibrationEmail instance.
        generated_draft: The draft text (or None if NO_DRAFT_NEEDED).
        ground_truth: GroundTruthScore from Layer 1.
        iteration: Current calibration iteration number.
        thread_summary: Cached thread summary text.
        api_key: Anthropic API key.

    Returns:
        CalibrationResult.
    """
    incoming_body = (cal_email.incoming_email.get("body") or "")[:3000]
    actual_reply = cal_email.user_reply
    logger.debug(
        f"[DRAFT-SCORE] {cal_email.db_id[:8]} iter={iteration}: "
        f"draft_len={len(generated_draft) if generated_draft else 0}, "
        f"reply_len={len(actual_reply) if actual_reply else 0}"
    )

    # Style delta (mechanical)
    if generated_draft:
        draft_style = _score_style(generated_draft)
        style_delta = _compute_style_delta(ground_truth.style, draft_style)
    else:
        style_delta = StyleDelta(
            word_count_ratio=0.0, greeting_match="not_applicable",
            signoff_match="not_applicable", contraction_match="match",
            bullet_match="match", formality_register="not_applicable",
        )

    # Behavioral delta
    if generated_draft:
        draft_behavioral = _score_draft_behavioral(incoming_body, generated_draft, api_key)
        behavioral_delta = _compute_behavioral_delta(
            ground_truth.behavioral, draft_behavioral,
        )
    else:
        behavioral_delta = BehavioralDelta(
            decisiveness_match="not_applicable",
            thoroughness_match="not_applicable",
            specificity_match="not_applicable",
        )

    # Preference delta
    if generated_draft:
        draft_preference = _score_draft_preference(
            incoming_body, generated_draft, thread_summary, api_key,
        )
        preference_delta = _compute_preference_delta(
            ground_truth.preference, draft_preference,
        )
    else:
        preference_delta = PreferenceDelta(
            investment_match="not_applicable",
            positional_match="not_applicable",
        )

    # Contextual scoring
    contextual = _score_contextual(
        cal_email, generated_draft, actual_reply,
        incoming_body, thread_summary, api_key,
    )

    # Overall classification
    overall = _classify_overall(style_delta, behavioral_delta,
                                 preference_delta, contextual)

    logger.debug(
        f"[DRAFT-SCORE] {cal_email.db_id[:8]} result: overall={overall}, "
        f"style_delta=(wc_ratio={style_delta.word_count_ratio}, "
        f"greeting={style_delta.greeting_match}, signoff={style_delta.signoff_match}, "
        f"formality={style_delta.formality_register}), "
        f"contextual=(draft_acc={contextual.should_draft_accuracy}, "
        f"content={contextual.content_alignment}, fab={contextual.fabrication_detected})"
    )

    return CalibrationResult(
        email_id=cal_email.db_id,
        iteration=iteration,
        ground_truth=ground_truth,
        generated_draft=generated_draft,
        actual_reply=actual_reply,
        incoming_body=incoming_body,
        thread_summary=thread_summary or "",
        style_delta=style_delta,
        behavioral_delta=behavioral_delta,
        preference_delta=preference_delta,
        contextual=contextual,
        overall=overall,
    )


# ---------------------------------------------------------------------------
# Style delta
# ---------------------------------------------------------------------------

def _compute_style_delta(actual, draft):
    """Compare draft style scores against ground truth."""
    # Word count ratio
    if actual.word_count > 0:
        ratio = draft.word_count / actual.word_count
    else:
        ratio = 1.0 if draft.word_count == 0 else 999.0

    greeting_match = _ordinal_match(
        actual.greeting_type, draft.greeting_type, _GREETING_ORDER,
    )
    signoff_match = _ordinal_match(
        actual.signoff_type, draft.signoff_type, _SIGNOFF_ORDER,
    )
    contraction_match = "match" if actual.uses_contractions == draft.uses_contractions else "mismatch"
    bullet_match = "match" if actual.uses_bullets == draft.uses_bullets else "mismatch"

    # Formality register (composite)
    formality_register = _compute_formality_register(actual, draft)

    return StyleDelta(
        word_count_ratio=round(ratio, 2),
        greeting_match=greeting_match,
        signoff_match=signoff_match,
        contraction_match=contraction_match,
        bullet_match=bullet_match,
        formality_register=formality_register,
    )


def _ordinal_match(actual_val, draft_val, order):
    """Match two values on an ordinal scale."""
    if actual_val == draft_val:
        return "match"
    if actual_val not in order or draft_val not in order:
        return "hard_miss"
    distance = abs(order.index(actual_val) - order.index(draft_val))
    return "adjacent" if distance == 1 else "hard_miss"


def _compute_formality_register(actual, draft):
    """Composite formality comparison."""
    # Simple heuristic: greeting + signoff + contractions
    actual_score = _formality_score(actual)
    draft_score = _formality_score(draft)
    diff = abs(actual_score - draft_score)
    if diff <= 1:
        return "match"
    if diff <= 2:
        return "adjacent"
    return "hard_miss"


def _formality_score(style):
    """Numeric formality score (higher = more formal)."""
    score = 0
    greeting_formality = {"none": 0, "minimal": 1, "professional": 2, "warm": 1}
    signoff_formality = {"none": 0, "simple_closing": 1, "signature_block": 2, "warm_closing": 1}
    score += greeting_formality.get(style.greeting_type, 0)
    score += signoff_formality.get(style.signoff_type, 0)
    if not style.uses_contractions:
        score += 1  # More formal
    return score


# ---------------------------------------------------------------------------
# Behavioral delta
# ---------------------------------------------------------------------------

def _score_draft_behavioral(incoming_body, draft_text, api_key=None):
    """Score a draft's behavioral dimensions."""
    prompt = BEHAVIORAL_SCORING_PROMPT.format(
        incoming_email_body=incoming_body,
        sent_email_body=draft_text[:3000],
    )
    try:
        text, _ = call_claude(
            prompt=prompt,
            model=resolve_model(MODEL),
            max_tokens=50,
            temperature=0,
            api_key=api_key,
        )
        return _parse_behavioral(text)
    except Exception as e:
        logger.warning(f"Draft behavioral scoring failed: {e}")
        return BehavioralScore("no_signal", "no_signal", "no_signal")


def _compute_behavioral_delta(actual, draft):
    """Compare behavioral classifications."""
    return BehavioralDelta(
        decisiveness_match=_behavioral_ordinal(
            actual.decisiveness, draft.decisiveness, _DECISIVENESS_ORDER,
        ),
        thoroughness_match=_behavioral_simple(
            actual.thoroughness, draft.thoroughness,
        ),
        specificity_match=_behavioral_ordinal(
            actual.specificity, draft.specificity, _SPECIFICITY_ORDER,
        ),
    )


def _behavioral_ordinal(actual_val, draft_val, order):
    """Match behavioral values on an ordinal scale, handling no_signal."""
    if actual_val == "no_signal" or draft_val == "no_signal":
        if actual_val == draft_val:
            return "match"
        return "adjacent"  # Can't penalize heavily when no signal
    return _ordinal_match(actual_val, draft_val, order)


def _behavioral_simple(actual_val, draft_val):
    """Simple match for binary behavioral dimensions."""
    if actual_val == draft_val:
        return "match"
    if actual_val == "no_signal" or draft_val == "no_signal":
        return "adjacent"
    return "hard_miss"


# ---------------------------------------------------------------------------
# Preference delta
# ---------------------------------------------------------------------------

_INVESTMENT_ORDER = ["active", "selective", "conservative"]
_POSITIONAL_ORDER = ["advancing", "measured", "yielding"]


def _score_draft_preference(incoming_body, draft_text, thread_summary, api_key=None):
    """Score a draft's preference dimensions."""
    prompt = PREFERENCE_SCORING_PROMPT.format(
        incoming_email_body=incoming_body,
        sent_email_body=draft_text[:3000],
        thread_summary_or_none=thread_summary or "None.",
    )
    try:
        text, _ = call_claude(
            prompt=prompt,
            model=resolve_model(MODEL),
            max_tokens=30,
            temperature=0,
            api_key=api_key,
        )
        return _parse_preference(text)
    except Exception as e:
        logger.warning(f"Draft preference scoring failed: {e}")
        return PreferenceScore("no_signal", "no_signal")


def _compute_preference_delta(actual, draft):
    """Compare preference classifications."""
    inv = _preference_match(
        actual.investment_signal, draft.investment_signal, _INVESTMENT_ORDER,
    )
    pos = _preference_match(
        actual.positional_signal, draft.positional_signal, _POSITIONAL_ORDER,
    )
    return PreferenceDelta(investment_match=inv, positional_match=pos)


def _preference_match(actual_val, draft_val, order):
    """Match preference values, returning not_applicable for no_signal pairs."""
    if actual_val == "no_signal" and draft_val == "no_signal":
        return "not_applicable"
    if actual_val == "no_signal" or draft_val == "no_signal":
        return "adjacent"
    return _ordinal_match(actual_val, draft_val, order)


# ---------------------------------------------------------------------------
# Contextual scoring
# ---------------------------------------------------------------------------

def _score_contextual(cal_email, generated_draft, actual_reply,
                      incoming_body, thread_summary, api_key=None):
    """Score contextual dimensions (should-draft + Opus-as-judge)."""
    user_replied = actual_reply is not None
    system_drafted = generated_draft is not None

    # Should-draft accuracy (mechanical)
    if (system_drafted and user_replied) or (not system_drafted and not user_replied):
        should_draft = "correct"
    else:
        should_draft = "incorrect"

    # If no draft or no reply, can't do content comparison
    if not generated_draft or not actual_reply:
        return ContextualScore(
            should_draft_accuracy=should_draft,
            content_alignment="not_applicable" if should_draft == "correct" else "hard_miss",
            fabrication_detected=False,
            comprehension_pass=True,
            attribution_pass=True,
        )

    # Opus-as-judge for content dimensions
    prompt = CONTEXTUAL_SCORING_PROMPT.format(
        incoming_email=incoming_body,
        thread_summary=thread_summary or "None.",
        actual_reply=actual_reply[:3000],
        generated_draft=generated_draft[:3000],
    )
    try:
        text, _ = call_claude(
            prompt=prompt,
            model=resolve_model(MODEL),
            max_tokens=80,
            temperature=0,
            api_key=api_key,
        )
        return _parse_contextual(text, should_draft)
    except Exception as e:
        logger.warning(f"Contextual scoring failed: {e}")
        return ContextualScore(
            should_draft_accuracy=should_draft,
            content_alignment="partial",
            fabrication_detected=False,
            comprehension_pass=True,
            attribution_pass=True,
        )


def _parse_contextual(text, should_draft):
    """Parse the structured contextual scoring response."""
    content = "partial"
    fabrication = False
    comprehension = True
    attribution = True

    for line in text.strip().splitlines():
        line = line.strip().upper()
        if line.startswith("CONTENT_ALIGNMENT:"):
            val = line.split(":", 1)[1].strip().split()[0].lower()
            if val in ("match", "partial", "hard_miss"):
                content = val
        elif line.startswith("FABRICATION:"):
            val = line.split(":", 1)[1].strip().split()[0].lower()
            fabrication = val == "detected"
        elif line.startswith("COMPREHENSION:"):
            val = line.split(":", 1)[1].strip().split()[0].lower()
            comprehension = val == "pass"
        elif line.startswith("ATTRIBUTION:"):
            val = line.split(":", 1)[1].strip().split()[0].lower()
            attribution = val == "pass"

    return ContextualScore(
        should_draft_accuracy=should_draft,
        content_alignment=content,
        fabrication_detected=fabrication,
        comprehension_pass=comprehension,
        attribution_pass=attribution,
    )


# ---------------------------------------------------------------------------
# Overall classification
# ---------------------------------------------------------------------------

def _classify_overall(style, behavioral, preference, contextual):
    """Determine overall result: pass | soft_miss | hard_miss."""
    # Any contextual failure = hard_miss
    if contextual.should_draft_accuracy == "incorrect":
        return "hard_miss"
    if contextual.fabrication_detected:
        return "hard_miss"
    if not contextual.comprehension_pass:
        return "hard_miss"
    if not contextual.attribution_pass:
        return "hard_miss"
    if contextual.content_alignment == "hard_miss":
        return "hard_miss"

    # Check for hard misses in style/behavioral/preference
    hard_miss_fields = [
        style.greeting_match, style.signoff_match, style.formality_register,
        behavioral.decisiveness_match, behavioral.thoroughness_match,
        behavioral.specificity_match,
        preference.investment_match, preference.positional_match,
    ]
    if any(f == "hard_miss" for f in hard_miss_fields):
        return "hard_miss"

    # Word count ratio check
    if style.word_count_ratio > 0:
        if style.word_count_ratio < 0.4 or style.word_count_ratio > 2.5:
            return "hard_miss"

    # Check for adjacent misses
    adjacent_fields = hard_miss_fields + [
        style.contraction_match, style.bullet_match,
    ]
    has_adjacent = any(
        f in ("adjacent", "mismatch")
        for f in adjacent_fields
        if f not in ("not_applicable",)
    )
    if has_adjacent:
        return "soft_miss"

    return "pass"


def result_to_dict(result):
    """Convert a CalibrationResult to a JSON-serializable dict for DB storage."""
    return {
        "email_id": result.email_id,
        "iteration": result.iteration,
        "ground_truth": asdict(result.ground_truth),
        "generated_draft": result.generated_draft,
        "actual_reply": result.actual_reply,
        "incoming_email_body": result.incoming_body,
        "thread_summary": result.thread_summary,
        "style_delta": asdict(result.style_delta),
        "behavioral_delta": asdict(result.behavioral_delta),
        "preference_delta": asdict(result.preference_delta),
        "contextual_scores": asdict(result.contextual),
        "overall_result": result.overall,
    }
