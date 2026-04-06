"""Generate correction rules from calibration hard misses."""

import logging
from collections import Counter
from dataclasses import asdict

from pipeline.api_client import call_claude, resolve_model
from calibration.prompts import CORRECTION_GENERATION_PROMPT

logger = logging.getLogger("worker.calibration")

MODEL = "opus"

# Severity weights for failure dimensions
_SEVERITY = {
    "fabrication_detected": 10,
    "comprehension_fail": 9,
    "attribution_fail": 8,
    "should_draft_incorrect": 7,
    "content_alignment_hard_miss": 6,
    "decisiveness_hard_miss": 4,
    "specificity_hard_miss": 4,
    "thoroughness_hard_miss": 3,
    "investment_hard_miss": 3,
    "positional_hard_miss": 3,
    "greeting_hard_miss": 2,
    "signoff_hard_miss": 2,
    "formality_hard_miss": 2,
    "word_count_extreme": 2,
    "contraction_mismatch": 1,
    "bullet_mismatch": 1,
}


def generate_corrections(results, api_key=None):
    """Analyze hard-miss results and generate correction rules.

    Args:
        results: list[CalibrationResult] from the current iteration.
        api_key: Anthropic API key.

    Returns:
        list[str]: Correction rules, ranked by severity × frequency, max 10.
    """
    hard_misses = [r for r in results if r.overall == "hard_miss"]
    if not hard_misses:
        logger.debug("[CORRECTION] no hard misses — skipping correction generation")
        return []

    logger.debug(f"[CORRECTION] analyzing {len(hard_misses)} hard misses for correction rules")

    # Classify failure dimensions for each hard miss
    failures = []
    failure_counts = Counter()

    for result in hard_misses:
        dims = _extract_failure_dimensions(result)
        for dim in dims:
            failure_counts[dim] += 1
        failures.append({
            "email_id": result.email_id,
            "dimensions": dims,
            "style_delta": asdict(result.style_delta),
            "behavioral_delta": asdict(result.behavioral_delta),
            "preference_delta": asdict(result.preference_delta),
            "contextual": asdict(result.contextual),
            "incoming_snippet": (result.incoming_body or "")[:500],
            "draft_snippet": (result.generated_draft or "")[:500],
            "actual_snippet": (result.actual_reply or "")[:500],
        })

    logger.debug(f"[CORRECTION] failure dimensions: {dict(failure_counts)}")

    # Build failure block for prompt
    failures_block = _build_failures_block(failures)
    logger.debug(f"[CORRECTION] failure block length: {len(failures_block)} chars")

    # Generate rules via Opus
    prompt = CORRECTION_GENERATION_PROMPT.format(failures_block=failures_block)
    try:
        text, _ = call_claude(
            prompt=prompt,
            model=resolve_model(MODEL),
            max_tokens=500,
            temperature=0,
            api_key=api_key,
        )
        raw_rules = _parse_rules(text)
        logger.debug(f"[CORRECTION] Opus generated {len(raw_rules)} raw rules")
    except Exception as e:
        logger.warning(f"Correction generation failed: {e}")
        raw_rules = _generate_mechanical_rules(failure_counts)
        logger.debug(f"[CORRECTION] fallback mechanical rules: {len(raw_rules)}")

    # Rank and trim
    final_rules = rank_and_trim_rules(raw_rules, failure_counts, max_rules=10)
    logger.debug(f"[CORRECTION] final rules after rank/trim: {len(final_rules)}")
    for i, rule in enumerate(final_rules):
        logger.debug(f"[CORRECTION] rule {i+1}: {rule[:100]}")
    return final_rules


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


def _build_failures_block(failures):
    """Format failures into a structured text block for the prompt."""
    lines = []
    for i, f in enumerate(failures, 1):
        lines.append(f"--- Failure {i} ---")
        lines.append(f"Dimensions: {', '.join(f['dimensions'])}")
        lines.append(f"Incoming email snippet: {f['incoming_snippet']}")
        if f["draft_snippet"]:
            lines.append(f"Generated draft snippet: {f['draft_snippet']}")
        if f["actual_snippet"]:
            lines.append(f"User's actual reply snippet: {f['actual_snippet']}")

        # Add specific delta info for context
        ctx = f["contextual"]
        if ctx.get("fabrication_detected"):
            lines.append("Issue: Draft fabricated information not in the email/thread.")
        if not ctx.get("comprehension_pass", True):
            lines.append("Issue: Draft misunderstood what was being asked.")
        if not ctx.get("attribution_pass", True):
            lines.append("Issue: Draft responded to wrong person or wrong topic.")

        sd = f["style_delta"]
        if sd.get("word_count_ratio", 1.0) > 2.5:
            lines.append(f"Issue: Draft was {sd['word_count_ratio']}x longer than user's actual reply.")
        elif sd.get("word_count_ratio", 1.0) < 0.4:
            lines.append(f"Issue: Draft was {sd['word_count_ratio']}x shorter than user's actual reply.")

        lines.append("")
    return "\n".join(lines)


def _parse_rules(text):
    """Parse correction rules from Opus response."""
    rules = []
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("- "):
            rule = line[2:].strip()
            if rule:
                rules.append(rule)
    return rules


def _generate_mechanical_rules(failure_counts):
    """Fallback: generate mechanical rules from failure dimensions."""
    rules = []
    templates = {
        "fabrication_detected": "Never introduce facts, commitments, deadlines, or references not present in the incoming email or thread.",
        "comprehension_fail": "Re-read the incoming email carefully before drafting; ensure the response addresses what was actually asked.",
        "attribution_fail": "Verify the draft responds to the correct person about the correct topic before finalizing.",
        "should_draft_incorrect": "Only generate a draft when the user would actually reply; skip routine FYIs, newsletters, and group messages where the user is CC'd.",
        "content_alignment_hard_miss": "Match the substantive decision the user would make — don't substitute a different conclusion or recommendation.",
        "word_count_extreme": "Keep draft length proportional to the user's typical reply length for similar emails.",
        "greeting_hard_miss": "Match the user's greeting style (formal vs. casual vs. none).",
        "signoff_hard_miss": "Match the user's sign-off style.",
        "formality_hard_miss": "Match the user's overall formality level.",
        "contraction_mismatch": "Match the user's contraction usage (use contractions if the user does, avoid them if the user doesn't).",
        "decisiveness_hard_miss": "Match the user's decision-making style — if they typically propose solutions, don't defer; if they defer, don't decide.",
        "specificity_hard_miss": "Match the user's specificity level — if they give specific next steps, do the same; if they stay general, don't over-specify.",
    }
    for dim, count in failure_counts.most_common():
        if dim in templates:
            rules.append(templates[dim])
    return rules


def rank_and_trim_rules(rules, failure_counts=None, max_rules=10):
    """Rank rules by estimated impact and trim to max_rules.

    Impact heuristic: rules associated with higher-severity and
    higher-frequency failures rank first.
    """
    if len(rules) <= max_rules:
        return rules

    if not failure_counts:
        return rules[:max_rules]

    # Score each rule by matching keywords to failure dimensions
    scored = []
    for rule in rules:
        score = 0
        rule_lower = rule.lower()
        for dim, count in failure_counts.items():
            severity = _SEVERITY.get(dim, 1)
            # Check if rule text relates to this dimension
            keywords = dim.replace("_", " ").split()
            if any(kw in rule_lower for kw in keywords):
                score += severity * count
        # Default score of 1 so all rules are considered
        scored.append((max(score, 1), rule))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [rule for _, rule in scored[:max_rules]]
