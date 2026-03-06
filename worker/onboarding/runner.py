"""Onboarding orchestrator: runs Phases 1-6 in dependency order.

Execution graph:
  Phase 1 (collect)           → status="collecting"
  Phase 2 (stats)             → status="statistics"
  Phase 3 + 4C-1 (parallel)  → status="extracting"
  Phase 4A (contacts)         → status="synthesizing"
  Phase 4B + 4C-2 (parallel) → status="style_guide"
  Phase 6 (write to DB)       → status="finalizing"
  Set completed_at            → status="complete"

Opus calibration (Phase 5) runs separately via run_calibration().
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from onboarding.collectors import collect_onboarding_emails
from onboarding.extraction import extract_email_features, extract_writing_styles
from onboarding.statistics import compute_all_statistics
from onboarding.synthesis import synthesize_contacts, synthesize_topics, synthesize_style_guide

logger = logging.getLogger("worker.onboarding")


def run_onboarding(db, user_id, profile):
    """Execute the full onboarding pipeline for a user.

    Args:
        db: SupabaseWorkerClient instance.
        user_id: UUID string.
        profile: User profile dict.

    Returns:
        bool: True if onboarding completed successfully.
    """
    aliases = profile.get("user_email_aliases") or []
    logger.info(f"Starting onboarding for user {user_id} (aliases: {aliases})")

    try:
        db.update_onboarding_status(
            user_id, "starting",
            started_at=datetime.utcnow().isoformat(),
        )

        # ── Phase 1: Collect ─────────────────────────────────────
        db.update_onboarding_status(user_id, "collecting")
        email_data = collect_onboarding_emails(db, user_id, aliases, days=30)
        received = email_data["received"]
        sent = email_data["sent"]

        if len(received) < 10:
            logger.warning(f"Only {len(received)} received emails — too few for onboarding")
            db.update_onboarding_status(user_id, "failed")
            return False

        # ── Phase 2: Statistics ──────────────────────────────────
        db.update_onboarding_status(user_id, "statistics")
        stats = compute_all_statistics(received, sent, aliases)
        logger.info("Phase 2 complete: statistics computed")

        # ── Phase 3 + 4C-1: Parallel Haiku extraction ───────────
        db.update_onboarding_status(user_id, "extracting")

        extraction_result = None
        style_result = None

        with ThreadPoolExecutor(max_workers=2) as executor:
            f_extract = executor.submit(
                extract_email_features,
                received,
                stats["contact_frequencies"],
            )
            f_style = executor.submit(
                extract_writing_styles,
                sent,
            )
            extraction_result = f_extract.result()
            style_result = f_style.result()

        if extraction_result is None:
            logger.error("Phase 3 extraction failed completely")
            db.update_onboarding_status(user_id, "failed")
            return False

        logger.info("Phase 3 + 4C-1 complete: Haiku extraction done")

        # ── Phase 4A: Contact profile synthesis ──────────────────
        db.update_onboarding_status(user_id, "synthesizing")

        contact_profiles = synthesize_contacts(
            stats["contact_frequencies"],
            stats["response_rates"],
            extraction_result.get("extractions", []),
        )

        if contact_profiles is None:
            # Fallback: use Python-only stats without Sonnet enrichment
            logger.warning("Sonnet contact synthesis failed, using stats-only profiles")
            contact_profiles = _fallback_contact_profiles(
                stats["contact_frequencies"],
                stats["response_rates"],
            )

        logger.info(f"Phase 4A complete: {len(contact_profiles)} contact profiles")

        # ── Phase 4B + 4C-2: Parallel Sonnet synthesis ───────────
        db.update_onboarding_status(user_id, "style_guide")

        topic_result = None
        style_guide = None

        with ThreadPoolExecutor(max_workers=2) as executor:
            f_topics = executor.submit(
                synthesize_topics,
                extraction_result.get("keyword_frequencies", {}),
            )
            f_guide = executor.submit(
                synthesize_style_guide,
                style_result.get("style_features", []) if style_result else [],
                contact_profiles,
            )
            topic_result = f_topics.result()
            style_guide = f_guide.result()

        if topic_result is None:
            topic_result = {"domains": [], "high_signal_keywords": []}

        logger.info("Phase 4B + 4C-2 complete: topics + style guide done")

        # ── Phase 6: Write to DB ─────────────────────────────────
        db.update_onboarding_status(user_id, "finalizing")

        # Merge stats into contact profiles for DB write
        contacts_for_db = _merge_stats_into_contacts(
            contact_profiles, stats, email_data
        )
        db.upsert_contacts(user_id, contacts_for_db)

        # Topic profile
        db.upsert_topic_profile(user_id, {
            "domains": topic_result.get("domains", []),
            "high_signal_keywords": topic_result.get("high_signal_keywords", []),
            "token_frequencies": stats.get("subject_tokens"),
            "baseline_statistics": stats.get("aggregate", {}),
        })

        # Writing style guide
        if style_guide:
            sample_count = style_result.get("sample_count", 0) if style_result else 0
            db.update_writing_style(user_id, style_guide, sample_count)

        # Mark complete
        db.update_onboarding_status(
            user_id, "complete",
            completed_at=datetime.utcnow().isoformat(),
        )

        logger.info(f"Onboarding complete for user {user_id}")
        return True

    except Exception:
        logger.exception(f"Onboarding failed for user {user_id}")
        db.update_onboarding_status(user_id, "failed")
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fallback_contact_profiles(contact_freq, response_rates):
    """Build minimal contact profiles from stats only (no Sonnet)."""
    profiles = []
    for sender, freq in contact_freq.items():
        rates = response_rates.get(sender, {})
        profiles.append({
            "email": sender,
            "inferred_organization": _org_from_domain(sender),
            "inferred_role": "unknown",
            "expertise_areas": [],
            "contact_type": "unknown",
            "relationship_significance": _infer_significance(
                freq["count"], rates.get("response_rate", 0)
            ),
            "relationship_summary": None,
        })
    return profiles


def _org_from_domain(email):
    """Infer organization name from email domain."""
    try:
        domain = email.split("@")[1]
        # Strip common suffixes
        name = domain.split(".")[0]
        return name.title()
    except (IndexError, AttributeError):
        return "unknown"


def _infer_significance(email_count, response_rate):
    """Heuristic significance from frequency + response rate."""
    if email_count >= 20 and response_rate >= 0.7:
        return "critical"
    if email_count >= 10 or response_rate >= 0.5:
        return "high"
    if email_count >= 5:
        return "medium"
    return "low"


def _merge_stats_into_contacts(contact_profiles, stats, email_data):
    """Merge Python-computed stats into contact profile dicts for DB write."""
    freq = stats.get("contact_frequencies", {})
    rates = stats.get("response_rates", {})

    total_days = stats.get("aggregate", {}).get("date_range_days", 30)

    merged = []
    for profile in contact_profiles:
        email = (profile.get("email") or "").lower()
        f = freq.get(email, {})
        r = rates.get(email, {})

        contact = dict(profile)
        contact["emails_per_month"] = round(
            f.get("count", 0) * 30 / max(total_days, 1)
        )
        contact["response_rate"] = r.get("response_rate")
        contact["avg_response_time_hours"] = r.get("avg_response_time_hours")
        contact["last_interaction_at"] = f.get("last_seen")
        contact["common_co_recipients"] = f.get("top_co_recipients", [])

        # Compute user_initiates_pct from sent emails
        sent_to_contact = sum(
            1 for s in email_data.get("sent", [])
            if email in (s.get("to_field") or "").lower()
        )
        total_with_contact = f.get("count", 0) + sent_to_contact
        if total_with_contact > 0:
            contact["user_initiates_pct"] = round(
                sent_to_contact / total_with_contact, 4
            )

        merged.append(contact)

    return merged
