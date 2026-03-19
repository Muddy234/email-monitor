"""Onboarding orchestrator – three-stage pipeline.

Stage 1 – Ingest & Persist (pure Python, no AI):
  Phase 1  (collect)           → status="collecting"
  Phase 2  (stats extraction)  → status="statistics"
  DB write (core data)         → status="persisting"
    Writes: response_events, threads, domains, contacts (stats-only),
            baseline user_topic_profile

Stage 2 – AI Enrichment:
  Phase 3 + 4C-1 (Haiku)      → status="extracting"
  Phase 4A  (Sonnet contacts)  → status="synthesizing"
    DB write: enriched contacts
  Phase 4B + 4C-2 (Sonnet)    → status="style_guide"
    DB write: topic domains, style guide

Stage 3 – Model Training:
  Phase 7  (train model)       → status="training"
  Set completed_at             → status="complete"

Design principle: each stage writes to the DB before the next stage begins.
If Stage 2 fails, Stage 1 data is already persisted — the user has a
functional (if un-enriched) system. If Stage 3 fails, enrichments are
still saved.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from onboarding.collectors import collect_onboarding_emails
from onboarding.extraction import (
    extract_email_features,
    extract_writing_styles,
    extract_behavioral_features,
    sample_unified_sent_emails,
)
from onboarding.stats_extraction import extract_all
from onboarding.model_trainer import train_user_model
from onboarding.synthesis import (
    synthesize_contacts,
    synthesize_topics,
    synthesize_style_guide,
    synthesize_behavioral_profile,
)

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

        # ================================================================
        # STAGE 1: Ingest & Persist  (pure Python, no AI)
        # ================================================================

        # ── Phase 1: Collect ─────────────────────────────────────
        db.update_onboarding_status(user_id, "collecting")
        email_data = collect_onboarding_emails(db, user_id, aliases, days=120, max_emails=400)
        received = email_data["received"]
        sent = email_data["sent"]

        if len(received) < 10:
            logger.warning(f"Only {len(received)} received emails — too few for onboarding")
            db.update_onboarding_status(user_id, "failed")
            return False

        # ── Phase 2: Full statistical extraction ──────────────────
        db.update_onboarding_status(user_id, "statistics")
        all_emails = received + sent
        extraction = extract_all(all_emails, aliases)
        stats = {
            "contact_frequencies": {
                s: {"count": c["total_received"]}
                for s, c in extraction["contacts"].items()
            },
            "response_rates": {
                s: {
                    "response_rate": c["reply_rate"],
                    "avg_response_time_hours": c["avg_response_time_hours"],
                }
                for s, c in extraction["contacts"].items()
            },
            "aggregate": extraction["user_profile"],
            "subject_tokens": {},  # Not computed in new extraction
        }
        logger.info(f"Phase 2 complete: {len(extraction['response_events'])} events, "
                    f"{len(extraction['contacts'])} contacts")

        # ── Persist Stage 1 data to DB ────────────────────────────
        db.update_onboarding_status(user_id, "persisting")
        stage1_failures = []

        # Response events
        try:
            db.upsert_response_events(user_id, extraction["response_events"])
        except Exception as e:
            logger.error(f"Stage 1: upsert_response_events failed: {e}")
            stage1_failures.append("response_events")

        # Threads
        try:
            db.upsert_threads(user_id, list(extraction["threads"].values()))
        except Exception as e:
            logger.error(f"Stage 1: upsert_threads failed: {e}")
            stage1_failures.append("threads")

        # Domains
        try:
            db.upsert_domains(user_id, list(extraction["domains"].values()))
        except Exception as e:
            logger.error(f"Stage 1: upsert_domains failed: {e}")
            stage1_failures.append("domains")

        # Contacts (stats-only — all contacts, no AI enrichment yet)
        try:
            stats_contacts = _build_stats_only_contacts(extraction["contacts"])
            if stats_contacts:
                db.upsert_contacts(user_id, stats_contacts)
                logger.info(f"Wrote {len(stats_contacts)} stats-only contacts")
        except Exception as e:
            logger.error(f"Stage 1: upsert_contacts (stats-only) failed: {e}")
            stage1_failures.append("contacts")

        # Baseline topic profile (user_profile + empty enrichment fields)
        try:
            db.upsert_topic_profile(user_id, {
                "domains": [],
                "high_signal_keywords": [],
                "token_frequencies": stats.get("subject_tokens"),
                "baseline_statistics": extraction["user_profile"],
            })
        except Exception as e:
            logger.error(f"Stage 1: upsert_topic_profile (baseline) failed: {e}")
            stage1_failures.append("topic_profile")

        if stage1_failures:
            logger.error(f"Stage 1: {len(stage1_failures)} writes failed: {stage1_failures}")
            db.update_onboarding_status(user_id, "failed")
            return False

        logger.info("Stage 1 complete: core data persisted")

        # ================================================================
        # STAGE 2: AI Enrichment  (Haiku + Sonnet)
        # ================================================================

        # ── Phase 3 + 4C-1 + 4C-1b: Parallel Haiku extraction ────
        db.update_onboarding_status(user_id, "extracting")

        extraction_result = None
        style_result = None
        behavioral_result = None

        # Derive user domain for contact_type heuristic
        user_email = profile.get("email") or (aliases[0] if aliases else "")
        user_domain = user_email.split("@")[1] if "@" in user_email else None

        # Unified sample for style + behavioral extraction
        unified_sample = sample_unified_sent_emails(sent, user_domain, max_count=120)
        logger.info(f"Unified sample: {len(unified_sample)} sent emails for style + behavioral")

        with ThreadPoolExecutor(max_workers=3) as executor:
            f_extract = executor.submit(
                extract_email_features,
                received,
                stats["contact_frequencies"],
            )
            f_style = executor.submit(
                extract_writing_styles,
                sent,
                pre_sampled=unified_sample,
            )
            f_behavioral = executor.submit(
                extract_behavioral_features,
                sent,
                extraction["response_events"],
                received,
                user_domain,
                pre_sampled=unified_sample,
            )

            try:
                extraction_result = f_extract.result()
            except Exception:
                logger.exception("Phase 3: email feature extraction raised")

            try:
                style_result = f_style.result()
            except Exception:
                logger.exception("Phase 4C-1: writing style extraction raised")

            try:
                behavioral_result = f_behavioral.result()
            except Exception:
                logger.exception("Phase 4C-1b: behavioral extraction raised")

        if extraction_result is None:
            logger.error("Phase 3 extraction failed completely")
            db.update_onboarding_status(user_id, "failed")
            return False

        logger.info("Phase 3 + 4C-1 + 4C-1b complete: Haiku extraction done")

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

        # Write enriched contacts to DB immediately
        try:
            contacts_for_db = _merge_extraction_into_contacts(
                contact_profiles, extraction["contacts"]
            )
            if contacts_for_db:
                db.upsert_contacts(user_id, contacts_for_db)
                logger.info(f"Wrote {len(contacts_for_db)} enriched contacts")
        except Exception as e:
            logger.error(f"Stage 2: upsert enriched contacts failed: {e}")
            # Non-fatal — stats-only contacts from Stage 1 are already in DB

        # ── Phase 4B + 4C-2 + 4C-3: Parallel Sonnet synthesis ────
        db.update_onboarding_status(user_id, "style_guide")

        topic_result = None
        style_guide = None
        behavioral_profile = None

        with ThreadPoolExecutor(max_workers=3) as executor:
            f_topics = executor.submit(
                synthesize_topics,
                extraction_result.get("keyword_frequencies", {}),
            )
            f_guide = executor.submit(
                synthesize_style_guide,
                style_result.get("style_features", []) if style_result else [],
                contact_profiles,
            )
            f_behavioral = executor.submit(
                synthesize_behavioral_profile,
                behavioral_result.get("behavioral_features", []) if behavioral_result else [],
                contact_profiles,
            )
            topic_result = f_topics.result()
            style_guide = f_guide.result()
            try:
                behavioral_profile = f_behavioral.result()
            except Exception:
                logger.exception("Phase 4C-3: behavioral profile synthesis raised")

        if topic_result is None:
            topic_result = {"domains": [], "high_signal_keywords": []}

        logger.info("Phase 4B + 4C-2 + 4C-3 complete: topics + style + behavioral done")

        # Write enrichment results to DB
        try:
            db.upsert_topic_profile(user_id, {
                "domains": topic_result.get("domains", []),
                "high_signal_keywords": topic_result.get("high_signal_keywords", []),
                "token_frequencies": stats.get("subject_tokens"),
                "baseline_statistics": extraction["user_profile"],
            })
        except Exception as e:
            logger.error(f"Stage 2: upsert_topic_profile (enriched) failed: {e}")

        try:
            if style_guide:
                sample_count = style_result.get("sample_count", 0) if style_result else 0
                db.update_writing_style(user_id, style_guide, sample_count)
        except Exception as e:
            logger.error(f"Stage 2: update_writing_style failed: {e}")

        try:
            if behavioral_profile:
                db.update_behavioral_profile(user_id, behavioral_profile)
        except Exception as e:
            logger.error(f"Stage 2: update_behavioral_profile failed: {e}")

        logger.info("Stage 2 complete: AI enrichments persisted")

        # ================================================================
        # STAGE 3: Model Training
        # ================================================================

        db.update_onboarding_status(user_id, "training")
        try:
            params = train_user_model(db, user_id)
            logger.info(f"Stage 3 complete: model trained "
                        f"(global_rate={params.get('meta', {}).get('global_rate', '?')})")
        except Exception:
            logger.exception("Stage 3: model training failed (non-fatal)")

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

def _build_stats_only_contacts(extraction_contacts):
    """Build contact records from Phase 2 stats only (no AI enrichment).

    Used in Stage 1 to persist all contacts immediately after stats extraction.
    These rows are later updated with AI-enriched fields in Stage 2.
    """
    contacts = []
    for sender, ext_data in extraction_contacts.items():
        contacts.append({
            "email": sender,
            "total_received": ext_data.get("total_received", 0),
            "emails_per_month": ext_data.get("emails_per_month", 0),
            "response_rate": ext_data.get("reply_rate"),
            "reply_rate_30d": ext_data.get("reply_rate_30d"),
            "reply_rate_90d": ext_data.get("reply_rate_90d"),
            "smoothed_rate": ext_data.get("smoothed_rate"),
            "avg_response_time_hours": ext_data.get("avg_response_time_hours"),
            "median_response_time_hours": ext_data.get("median_response_time_hours"),
            "user_initiates_pct": ext_data.get("user_initiates_pct"),
            "forward_rate": ext_data.get("forward_rate"),
            "typical_subjects": ext_data.get("typical_subjects", []),
            "last_interaction_at": ext_data.get("last_seen"),
            "contact_type": ext_data.get("contact_type", "external"),
            "relationship_significance": _infer_significance(
                ext_data.get("total_received", 0),
                ext_data.get("reply_rate", 0),
            ),
            "inferred_organization": _org_from_domain(sender),
        })
    return contacts


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


def _merge_extraction_into_contacts(contact_profiles, extraction_contacts):
    """Merge extraction stats into Sonnet-synthesized contact profiles for DB write."""
    merged = []
    for profile in contact_profiles:
        email = (profile.get("email") or "").lower()
        ext = extraction_contacts.get(email, {})

        contact = dict(profile)
        contact["total_received"] = ext.get("total_received")
        contact["emails_per_month"] = ext.get("emails_per_month")
        contact["response_rate"] = ext.get("reply_rate")
        contact["avg_response_time_hours"] = ext.get("avg_response_time_hours")
        contact["median_response_time_hours"] = ext.get("median_response_time_hours")
        contact["last_interaction_at"] = ext.get("last_seen")
        contact["common_co_recipients"] = ext.get("co_recipients_top5", [])
        contact["user_initiates_pct"] = ext.get("user_initiates_pct")
        contact["reply_rate_30d"] = ext.get("reply_rate_30d")
        contact["reply_rate_90d"] = ext.get("reply_rate_90d")
        contact["smoothed_rate"] = ext.get("smoothed_rate")
        contact["forward_rate"] = ext.get("forward_rate")
        contact["typical_subjects"] = ext.get("typical_subjects", [])

        merged.append(contact)

    return merged
