"""Generate correction rules from calibration hard misses."""

import logging
import re

from pipeline.api_client import call_claude, resolve_model
from calibration.prompts import CORRECTION_GENERATION_PROMPT

logger = logging.getLogger("worker.calibration")

MODEL = "opus"
MAX_TOTAL_RULES = 10

# Failure dimensions that the draft prompt cannot influence and should be
# excluded from correction generation (they reflect upstream pipeline decisions
# or scoring artifacts, not draft-prompt deficiencies).
_NON_ACTIONABLE_DIMENSIONS = {
    "should_draft_incorrect",
}

# Phrases that indicate the LLM did not produce a real instruction.
_REJECT_PHRASES = (
    "no instruction",
    "cannot identify",
    "cannot determine",
    "no missing instruction",
    "no specific instruction",
    "no_gap",
    "no gap",
    "n/a",
    "none",
    "no change",
)


def generate_corrections(results, personality_profile, api_key=None):
    """Analyze hard-miss results and generate profile-refinement instructions.

    For each hard-miss email, ask Opus what instruction is missing from the
    personality profile that would have caused the draft to match the user's
    actual reply. One Opus call per failure.

    Args:
        results: list[CalibrationResult] from the current iteration.
        personality_profile: str — concatenated profile string passed to the
            draft generator.
        api_key: Anthropic API key.

    Returns:
        list[str]: Profile-refinement instructions, deduped.
    """
    hard_misses = [r for r in results if r.overall == "hard_miss"]
    if not hard_misses:
        logger.debug("[CORRECTION] no hard misses — skipping correction generation")
        return []

    logger.debug(
        f"[CORRECTION] analyzing {len(hard_misses)} hard misses against personality profile"
    )

    instructions = []
    for result in hard_misses:
        dims = _extract_failure_dimensions(result)
        actionable = [d for d in dims if d not in _NON_ACTIONABLE_DIMENSIONS]
        if not actionable:
            logger.debug(
                f"[CORRECTION] {result.email_id[:8]}: only non-actionable dims {dims}, skipping"
            )
            continue

        if not result.generated_draft or not result.actual_reply:
            logger.debug(
                f"[CORRECTION] {result.email_id[:8]}: missing draft or reply, skipping"
            )
            continue

        try:
            prompt = CORRECTION_GENERATION_PROMPT.format(
                personality_profile=personality_profile or "(empty profile)",
                actual_reply=result.actual_reply[:3000],
                generated_draft=result.generated_draft[:3000],
                failing_dimensions=", ".join(actionable),
            )
            text, _ = call_claude(
                prompt=prompt,
                model=resolve_model(MODEL),
                max_tokens=300,
                temperature=0,
                api_key=api_key,
            )
        except Exception as e:
            logger.warning(f"Correction generation failed for {result.email_id[:8]}: {e}")
            continue

        for line in _split_instructions(text):
            if _is_actionable(line):
                instructions.append(line)

    deduped = _dedupe(instructions)
    logger.debug(
        f"[CORRECTION] generated {len(instructions)} raw instructions, "
        f"{len(deduped)} after dedupe"
    )
    for i, rule in enumerate(deduped):
        logger.debug(f"[CORRECTION] rule {i+1}: {rule}")
    return deduped


def _extract_failure_dimensions(result):
    """Identify which dimensions caused a hard miss."""
    dims = []
    ctx = result.contextual

    if ctx.fabrication_detected:
        dims.append("fabrication_detected")
    if not ctx.comprehension_pass:
        dims.append("comprehension_fail")
    if not ctx.attribution_pass:
        dims.append("attribution_fail")
    if ctx.should_draft_accuracy == "incorrect":
        dims.append("should_draft_incorrect")
    if ctx.content_alignment == "hard_miss":
        dims.append("content_alignment_hard_miss")

    sd = result.style_delta
    if sd.greeting_match == "hard_miss":
        dims.append("greeting_hard_miss")
    if sd.signoff_match == "hard_miss":
        dims.append("signoff_hard_miss")
    if sd.formality_register == "hard_miss":
        dims.append("formality_hard_miss")
    if sd.word_count_ratio > 0 and (sd.word_count_ratio < 0.4 or sd.word_count_ratio > 2.5):
        dims.append("word_count_extreme")
    if sd.contraction_match == "mismatch":
        dims.append("contraction_mismatch")
    if sd.bullet_match == "mismatch":
        dims.append("bullet_mismatch")

    bd = result.behavioral_delta
    if bd.decisiveness_match == "hard_miss":
        dims.append("decisiveness_hard_miss")
    if bd.thoroughness_match == "hard_miss":
        dims.append("thoroughness_hard_miss")
    if bd.specificity_match == "hard_miss":
        dims.append("specificity_hard_miss")

    pd = result.preference_delta
    if pd.investment_match == "hard_miss":
        dims.append("investment_hard_miss")
    if pd.positional_match == "hard_miss":
        dims.append("positional_hard_miss")

    return dims


def _split_instructions(text):
    """Split LLM output into individual instructions."""
    if not text:
        return []
    cleaned = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip common list prefixes
        for prefix in ("- ", "* ", "• "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        # Strip numeric prefixes like "1. " or "1) "
        if len(line) > 2 and line[0].isdigit() and line[1] in (".", ")"):
            line = line[2:].strip()
        if line:
            cleaned.append(line)
    return cleaned


def _is_actionable(instruction):
    """Reject empty or non-instruction outputs."""
    if not instruction or len(instruction) < 10:
        return False
    lower = instruction.lower()
    for phrase in _REJECT_PHRASES:
        if lower.startswith(phrase) or lower == phrase:
            return False
    return True


def _dedupe(instructions):
    """Remove exact and near-duplicate instructions, preserving order.

    Uses normalized full text + 8-word fingerprint for near-dedup,
    then caps at MAX_TOTAL_RULES.
    """
    seen_keys = set()
    seen_fingerprints = set()
    out = []
    for inst in instructions:
        # Normalize: lowercase, strip punctuation, collapse whitespace
        normalized = " ".join(inst.lower().split())
        key = re.sub(r'[^\w\s]', '', normalized)
        if key in seen_keys:
            continue

        # 8-word fingerprint catches semantically similar rules
        words = key.split()
        fingerprint = " ".join(words[:8])
        if fingerprint in seen_fingerprints:
            continue

        seen_keys.add(key)
        seen_fingerprints.add(fingerprint)
        out.append(inst)

    return out[:MAX_TOTAL_RULES]
