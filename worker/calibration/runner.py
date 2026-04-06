"""Calibration runner — orchestrates the full calibration loop.

Selects 15 stratified test emails, scores ground truth, generates drafts
via the production pipeline, compares results, generates correction rules,
and iterates until exit thresholds are met (max 3 iterations).
"""

import logging
from datetime import datetime, timezone
from pipeline.api_client import submit_and_wait
from pipeline.drafts import DraftGenerator
from pipeline.signal_extractor import thread_summary_batch_params
from calibration.email_selector import select_calibration_emails
from calibration.ground_truth_scorer import score_ground_truth
from calibration.draft_scorer import score_draft, result_to_dict
from calibration.correction_generator import generate_corrections

logger = logging.getLogger("worker.calibration")

MAX_ITERATIONS = 3

# Exit thresholds (percentage of 15 emails that must pass per dimension)
THRESHOLDS = {
    "style": 0.90,       # ≥90% match or adjacent
    "behavioral": 0.80,  # ≥80%
    "preference": 0.75,  # ≥75%
    "contextual": 0.85,  # ≥85%
}


def run_calibration(db, user_id, api_key=None):
    """Run the full calibration loop for a user.

    Args:
        db: SupabaseWorkerClient instance.
        user_id: UUID string.
        api_key: Anthropic API key.

    Returns:
        bool: True if calibration passed, False otherwise.
    """
    logger.info(f"Starting calibration for user {user_id[:8]}...")
    db.update_calibration_status(user_id, "running")

    profile = db.fetch_user_config(user_id)
    aliases = profile.get("user_email_aliases", [])
    logger.debug(f"[CAL] user aliases: {aliases}")

    # Select 15 stratified test emails
    cal_emails = select_calibration_emails(db, user_id, aliases)
    if not cal_emails:
        logger.warning(f"No calibration emails found for {user_id[:8]}...")
        db.update_calibration_status(user_id, "needs_review")
        return False

    logger.info(f"Selected {len(cal_emails)} calibration emails")
    for ce in cal_emails:
        logger.debug(
            f"[CAL] cal_email: {ce.db_id[:8]}... contact_type={ce.contact_type}, "
            f"depth={ce.thread_depth}, has_reply={ce.user_reply is not None}, "
            f"buckets={ce.selection_buckets}"
        )

    # Score ground truth (Layer 1) — runs once
    ground_truths = {}
    for cal_email in cal_emails:
        gt = score_ground_truth(cal_email, api_key=api_key)
        if gt:
            ground_truths[cal_email.db_id] = gt
            logger.debug(
                f"[CAL] GT {cal_email.db_id[:8]}: style=({gt.style.greeting_type}/{gt.style.signoff_type}/{gt.style.word_count}w), "
                f"behavioral=({gt.behavioral.decisiveness}/{gt.behavioral.thoroughness}/{gt.behavioral.specificity}), "
                f"preference=({gt.preference.investment_signal}/{gt.preference.positional_signal})"
            )
        else:
            logger.debug(f"[CAL] GT {cal_email.db_id[:8]}: no ground truth returned")

    logger.info(f"Ground truth scored for {len(ground_truths)}/{len(cal_emails)} emails")

    # Cache thread summaries (generated once, reused across iterations)
    cached_thread_summaries = _generate_thread_summaries(
        cal_emails, profile, api_key,
    )

    # Build draft generator from profile
    config = _build_calibration_config(profile)
    draft_gen = DraftGenerator(config)

    # Accumulate correction rules across iterations
    all_rules = []
    iteration_results = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"Calibration iteration {iteration}/{MAX_ITERATIONS}")
        db.update_calibration_iteration(user_id, iteration)

        # Generate drafts for all test emails
        logger.debug(f"[CAL] iter {iteration}: generating drafts with {len(all_rules)} correction rules")
        drafts = _generate_calibration_drafts(
            cal_emails, draft_gen, profile, cached_thread_summaries,
            all_rules, api_key,
        )
        draft_count = sum(1 for d in drafts.values() if d is not None)
        logger.debug(f"[CAL] iter {iteration}: {draft_count}/{len(drafts)} drafts generated")

        # Score drafts against ground truth (Layer 2)
        results = []
        for cal_email in cal_emails:
            eid = cal_email.db_id
            gt = ground_truths.get(eid)
            if not gt:
                continue
            draft_text = drafts.get(eid)
            thread_summary = cached_thread_summaries.get(eid, "")

            result = score_draft(
                cal_email, draft_text, gt, iteration,
                thread_summary, api_key=api_key,
            )
            results.append(result)
            logger.debug(
                f"[CAL] iter {iteration} score {eid[:8]}: overall={result.overall}, "
                f"style=({result.style_delta.greeting_match}/{result.style_delta.signoff_match}/{result.style_delta.formality_register}), "
                f"behavioral=({result.behavioral_delta.decisiveness_match}/{result.behavioral_delta.thoroughness_match}), "
                f"contextual=(draft_acc={result.contextual.should_draft_accuracy}, fab={result.contextual.fabrication_detected})"
            )

        # Store results in DB
        _store_results(db, user_id, results, all_rules)
        iteration_results.append(results)

        # Check exit thresholds
        scores = _compute_dimension_scores(results)
        logger.info(
            f"Iteration {iteration} scores: "
            f"style={scores['style']:.0%} behavioral={scores['behavioral']:.0%} "
            f"preference={scores['preference']:.0%} contextual={scores['contextual']:.0%} "
            f"overall_pass={scores['overall_pass_rate']:.0%}"
        )

        if _thresholds_met(scores):
            logger.info(f"Calibration PASSED at iteration {iteration}")
            _finalize_calibration(db, user_id, all_rules, "passed")
            return True

        logger.debug(
            f"[CAL] iter {iteration}: thresholds NOT met — "
            f"needed style≥{THRESHOLDS['style']:.0%}, behavioral≥{THRESHOLDS['behavioral']:.0%}, "
            f"preference≥{THRESHOLDS['preference']:.0%}, contextual≥{THRESHOLDS['contextual']:.0%}"
        )

        # Generate correction rules from hard misses
        if iteration < MAX_ITERATIONS:
            hard_miss_count = sum(1 for r in results if r.overall == "hard_miss")
            soft_miss_count = sum(1 for r in results if r.overall == "soft_miss")
            logger.debug(f"[CAL] iter {iteration}: {hard_miss_count} hard_miss, {soft_miss_count} soft_miss")
            new_rules = generate_corrections(results, api_key=api_key)
            if new_rules:
                all_rules.extend(new_rules)
                # Deduplicate while preserving order
                seen = set()
                deduped = []
                for rule in all_rules:
                    if rule not in seen:
                        seen.add(rule)
                        deduped.append(rule)
                all_rules = deduped
                logger.info(f"Generated {len(new_rules)} new rules, total: {len(all_rules)}")
            else:
                logger.warning("No new correction rules generated")

    # Failed to meet thresholds after max iterations
    logger.warning(f"Calibration did not pass after {MAX_ITERATIONS} iterations")
    _finalize_calibration(db, user_id, all_rules, "needs_review")
    return False


# ---------------------------------------------------------------------------
# Thread summary generation
# ---------------------------------------------------------------------------

def _generate_thread_summaries(cal_emails, profile, api_key):
    """Generate thread summaries for all calibration emails (batch).

    Returns:
        dict: {email_id: summary_text}
    """
    summaries = {}
    requests = []
    user_aliases = profile.get("user_email_aliases", [])
    user_email = user_aliases[0] if user_aliases else ""
    logger.debug(f"[CAL] generating thread summaries for {len(cal_emails)} emails")

    for cal_email in cal_emails:
        eid = cal_email.db_id
        thread_emails = cal_email.thread_emails
        if not thread_emails:
            summaries[eid] = "No prior thread history."
            continue

        # Build thread summary request
        subject = cal_email.incoming_email.get("subject", "(no subject)")
        params = thread_summary_batch_params(
            subject, thread_emails, user_email=user_email, custom_id=eid,
        )
        if params:
            requests.append(params)
        else:
            summaries[eid] = "No prior thread history."

    if requests:
        logger.debug(f"[CAL] submitting {len(requests)} thread summary requests as batch")
        try:
            results, _ = submit_and_wait(requests, api_key=api_key)
            for custom_id, text in results.items():
                if text:
                    summaries[custom_id] = text
                else:
                    summaries[custom_id] = "No prior thread history."
            logger.debug(f"[CAL] thread summaries received: {len(results)}")
        except Exception as e:
            logger.warning(f"Thread summary batch failed: {e}")
            for req in requests:
                summaries[req["custom_id"]] = "No prior thread history."

    logger.debug(f"[CAL] total thread summaries: {len(summaries)}")
    return summaries


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def _generate_calibration_drafts(cal_emails, draft_gen, profile,
                                  thread_summaries, rules, api_key):
    """Generate drafts for all calibration emails using the production pipeline.

    Returns:
        dict: {email_id: draft_text_or_None}
    """
    style_guide = profile.get("writing_style_guide") or ""
    behavioral_profile = profile.get("behavioral_profile") or ""
    preference_profile = profile.get("preference_profile") or ""

    requests = []
    for cal_email in cal_emails:
        eid = cal_email.db_id
        email_data = cal_email.incoming_email
        thread_summary = thread_summaries.get(eid, "No prior thread history.")

        action_context = {
            "action": "reply",
            "context": "",
            "user_aliases": profile.get("user_email_aliases", []),
            "thread_summary": thread_summary,
        }
        if style_guide:
            action_context["style_guide"] = style_guide
        if behavioral_profile:
            action_context["behavioral_profile"] = behavioral_profile
        if preference_profile:
            action_context["preference_profile"] = preference_profile
        if rules:
            action_context["calibration_rules"] = rules

        req = draft_gen.build_batch_params(email_data, action_context, eid)
        requests.append(req)

    # Submit batch
    drafts = {}
    if requests:
        try:
            results, _ = submit_and_wait(requests, api_key=api_key)
            for eid, text in results.items():
                if text:
                    cleaned = draft_gen._validate_output(text, {})
                    drafts[eid] = cleaned
                else:
                    drafts[eid] = None
        except Exception as e:
            logger.warning(f"Calibration draft batch failed: {e}")
            # Fallback: generate one-by-one
            for cal_email in cal_emails:
                eid = cal_email.db_id
                email_data = cal_email.incoming_email
                thread_summary = thread_summaries.get(eid, "No prior thread history.")
                action_context = {
                    "action": "reply",
                    "context": "",
                    "user_aliases": profile.get("user_email_aliases", []),
                    "thread_summary": thread_summary,
                }
                if style_guide:
                    action_context["style_guide"] = style_guide
                if behavioral_profile:
                    action_context["behavioral_profile"] = behavioral_profile
                if preference_profile:
                    action_context["preference_profile"] = preference_profile
                if rules:
                    action_context["calibration_rules"] = rules

                try:
                    result, _, _ = draft_gen.generate_draft(email_data, action_context)
                    drafts[eid] = result
                except Exception as inner_e:
                    logger.warning(f"Draft generation failed for {eid}: {inner_e}")
                    drafts[eid] = None

    return drafts


# ---------------------------------------------------------------------------
# Scoring and thresholds
# ---------------------------------------------------------------------------

def _compute_dimension_scores(results):
    """Compute per-dimension pass rates from a set of CalibrationResults."""
    if not results:
        return {
            "style": 0.0, "behavioral": 0.0,
            "preference": 0.0, "contextual": 0.0,
            "overall_pass_rate": 0.0,
        }

    n = len(results)

    # Style: count emails where all style fields are match or adjacent (not hard_miss)
    style_pass = sum(1 for r in results if _style_passes(r.style_delta))
    # Behavioral: count emails where all behavioral fields pass
    behavioral_pass = sum(1 for r in results if _behavioral_passes(r.behavioral_delta))
    # Preference: count emails where all preference fields pass
    preference_pass = sum(1 for r in results if _preference_passes(r.preference_delta))
    # Contextual: all contextual checks pass
    contextual_pass = sum(1 for r in results if _contextual_passes(r.contextual))
    # Overall
    overall_pass = sum(1 for r in results if r.overall == "pass")

    return {
        "style": style_pass / n,
        "behavioral": behavioral_pass / n,
        "preference": preference_pass / n,
        "contextual": contextual_pass / n,
        "overall_pass_rate": overall_pass / n,
    }


def _style_passes(sd):
    """Check if style delta passes (no hard_miss)."""
    fields = [sd.greeting_match, sd.signoff_match, sd.formality_register]
    if any(f == "hard_miss" for f in fields):
        return False
    if sd.word_count_ratio > 0 and (sd.word_count_ratio < 0.4 or sd.word_count_ratio > 2.5):
        return False
    return True


def _behavioral_passes(bd):
    """Check if behavioral delta passes (no hard_miss)."""
    fields = [bd.decisiveness_match, bd.thoroughness_match, bd.specificity_match]
    return not any(f == "hard_miss" for f in fields)


def _preference_passes(pd):
    """Check if preference delta passes (no hard_miss)."""
    fields = [pd.investment_match, pd.positional_match]
    return not any(f == "hard_miss" for f in fields)


def _contextual_passes(ctx):
    """Check if contextual score passes."""
    if ctx.should_draft_accuracy == "incorrect":
        return False
    if ctx.fabrication_detected:
        return False
    if not ctx.comprehension_pass:
        return False
    if not ctx.attribution_pass:
        return False
    if ctx.content_alignment == "hard_miss":
        return False
    return True


def _thresholds_met(scores):
    """Check if all dimension scores meet exit thresholds."""
    return all(
        scores.get(dim, 0) >= threshold
        for dim, threshold in THRESHOLDS.items()
    )


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _store_results(db, user_id, results, rules_applied):
    """Store calibration results in the database."""
    rows = []
    for result in results:
        row = result_to_dict(result)
        row["user_id"] = user_id
        row["correction_rules_applied"] = rules_applied or []
        rows.append(row)

    if rows:
        db.store_calibration_results(rows)


def _finalize_calibration(db, user_id, rules, status):
    """Finalize calibration: store rules and update status."""
    rules_text = "\n".join(f"- {r}" for r in rules) if rules else None
    db.update_calibration_status(user_id, status, rules=rules_text)
    logger.info(
        f"Calibration finalized: status={status}, "
        f"rules={len(rules) if rules else 0}"
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _build_calibration_config(profile):
    """Build a DraftGenerator config from the user's profile."""
    import os
    return {
        "draft_model": os.environ.get("DRAFT_MODEL", "opus"),
        "draft_cli_timeout_seconds": 120,
        "draft_user_name": profile.get("display_name", ""),
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY"),
    }
