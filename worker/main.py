"""Railway pipeline worker — entry point.

Accumulates emails over an adaptive time window, then processes them
in batch via the Anthropic Message Batches API (50% cost discount).

Adaptive window logic:
- Starts at INITIAL_WINDOW (3 minutes).
- If no emails arrive during a window, the next window doubles in length.
- Caps at MAX_WINDOW (1 hour).
- Resets to INITIAL_WINDOW whenever emails are found.

Within each window the worker polls Supabase every POLL_INTERVAL seconds,
claiming emails as they arrive. Once the window closes:
  1. Filter all accumulated emails (rule-based + signal auto-skip).
  2. Submit classification batch via Batches API → poll until done.
  3. Process results, collect draft candidates.
  4. Submit draft batch via Batches API → poll until done.
  5. Write drafts to Supabase.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from supabase_client import SupabaseWorkerClient
from run_pipeline import (
    build_config_from_profile,
    filter_emails,
    process_classification_results,
)
from pipeline.analyzer import ClaudeAnalyzer
from pipeline.drafts import DraftGenerator
from pipeline.prompts import get_analysis_prompt, get_draft_prompt_template
from pipeline.api_client import submit_and_wait
from onboarding import run_onboarding, run_calibration

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
INITIAL_WINDOW = int(os.environ.get("INITIAL_WINDOW_SECONDS", "180"))  # 3 min
MAX_WINDOW = int(os.environ.get("MAX_WINDOW_SECONDS", "3600"))  # 1 hour
BATCH_POLL_INTERVAL = int(os.environ.get("BATCH_POLL_INTERVAL", "10"))
BATCH_MAX_WAIT = int(os.environ.get("BATCH_MAX_WAIT", "900"))  # 15 min

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# Heartbeat staleness threshold (seconds) — 3 missed 5-min sync cycles
HEARTBEAT_STALE_SECONDS = int(os.environ.get("HEARTBEAT_STALE_SECONDS", "900"))


# ---------------------------------------------------------------------------
# Activity detection
# ---------------------------------------------------------------------------


def _parse_iso(ts):
    """Parse an ISO timestamp string to a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _to_user_local(utc_now, tz_name):
    """Convert UTC datetime to the user's local time."""
    try:
        from zoneinfo import ZoneInfo
        return utc_now.astimezone(ZoneInfo(tz_name))
    except Exception:
        return utc_now


def _is_user_active(profile):
    """Check if a user is active based on heartbeat + business hours.

    Returns True if the worker should process this user's emails.
    Logic:
      1. worker_active=false → always skip (explicit logout)
      2. No heartbeat data → allow (backward compat / pre-migration)
      3. Heartbeat < 15 min old → active
      4. Stale heartbeat + M-F 7AM-6PM user's timezone → still active
      5. Stale heartbeat + outside business hours → skip
    """
    if not profile.get("worker_active", False):
        return False

    last_hb = profile.get("last_heartbeat_at")
    if not last_hb:
        return True

    now = datetime.now(timezone.utc)
    hb_time = _parse_iso(last_hb)
    if not hb_time:
        return True

    staleness = (now - hb_time).total_seconds()
    if staleness < HEARTBEAT_STALE_SECONDS:
        return True

    # Stale heartbeat — check business hours as fallback
    user_tz = profile.get("timezone", "America/Chicago")
    user_now = _to_user_local(now, user_tz)

    if user_now.weekday() < 5 and 7 <= user_now.hour < 18:
        return True

    return False


# ---------------------------------------------------------------------------
# Accumulation phase
# ---------------------------------------------------------------------------


def accumulate_emails(db, window_seconds):
    """Poll for emails over the window, claiming them as they arrive.

    Returns:
        dict: {user_id: {"profile": profile, "emails": [rows]}}
    """
    accumulated = {}  # user_id → {"profile": ..., "emails": [...]}
    deadline = time.time() + window_seconds
    poll_count = 0

    while time.time() < deadline and not _shutdown:
        poll_count += 1
        user_ids = db.get_users_with_unprocessed()

        for user_id in user_ids:
            if _shutdown:
                break

            # Get or cache profile
            if user_id not in accumulated:
                profile = db.fetch_user_config(user_id)
                if not profile or not _is_user_active(profile):
                    continue
                accumulated[user_id] = {"profile": profile, "emails": []}

            claimed = db.claim_unprocessed_emails(user_id, limit=BATCH_SIZE)
            if claimed:
                accumulated[user_id]["emails"].extend(claimed)
                logger.info(
                    f"  Poll {poll_count}: claimed {len(claimed)} for user {user_id[:8]}... "
                    f"(total: {len(accumulated[user_id]['emails'])})"
                )

        # Sleep in 1s increments for responsive shutdown
        for _ in range(POLL_INTERVAL):
            if _shutdown or time.time() >= deadline:
                break
            time.sleep(1)

    return accumulated


# ---------------------------------------------------------------------------
# Batch processing phase
# ---------------------------------------------------------------------------


def process_user_batch(db, user_id, profile, emails):
    """Process all accumulated emails for a single user via Batches API.

    Falls back to sync processing if batch submission fails.
    """
    config = build_config_from_profile(profile)
    run_id = db.create_pipeline_run(user_id, trigger_type="scheduled")

    try:
        # Step 1: Filter
        filtered = filter_emails(db, emails, user_id, config)
        if not filtered:
            logger.info(f"  User {user_id[:8]}...: all emails filtered out")
            db.update_pipeline_run(run_id, status="completed",
                                   emails_scanned=len(emails), emails_processed=0, drafts_generated=0)
            return 0, 0

        # Step 2: Classification via Batches API
        analyzer = ClaudeAnalyzer(config, system_prompt=get_analysis_prompt())
        api_key = config.get("anthropic_api_key")

        classify_request = analyzer.build_batch_params(filtered)
        logger.info(f"  User {user_id[:8]}...: submitting classification batch ({len(filtered)} emails)")

        try:
            batch_results = submit_and_wait(
                [classify_request],
                api_key=api_key,
                poll_interval=BATCH_POLL_INTERVAL,
                max_wait=BATCH_MAX_WAIT,
            )
            raw_text = batch_results.get("classification")
        except Exception as e:
            logger.warning(f"  Batch classification failed, falling back to sync: {e}")
            raw_text = None

        # Parse classification results (batch or sync fallback)
        if raw_text:
            action_items = analyzer.parse_batch_result(raw_text, filtered)
        else:
            logger.info(f"  User {user_id[:8]}...: using sync classification fallback")
            action_items = analyzer.analyze_batch(filtered)

        if not action_items:
            logger.warning(f"  User {user_id[:8]}...: classification returned no results")
            for ed in filtered:
                db.update_email_status(ed["_db_id"], "completed")
            db.update_pipeline_run(run_id, status="completed",
                                   emails_scanned=len(emails), emails_processed=len(filtered), drafts_generated=0)
            return len(filtered), 0

        # Step 3: Process classifications, collect draft candidates
        draft_generator = DraftGenerator(config, system_prompt_template=get_draft_prompt_template())
        emails_processed, draft_candidates = process_classification_results(
            db, action_items, filtered, user_id, config, draft_generator
        )

        # Step 4: Draft generation via Batches API
        drafts_generated = 0
        if draft_candidates:
            draft_requests = []
            for candidate in draft_candidates:
                req = draft_generator.build_batch_params(
                    candidate["email_data"],
                    candidate["action_context"],
                    custom_id=candidate["db_id"],
                )
                draft_requests.append(req)

            logger.info(f"  User {user_id[:8]}...: submitting draft batch ({len(draft_requests)} drafts)")

            try:
                draft_results = submit_and_wait(
                    draft_requests,
                    api_key=api_key,
                    poll_interval=BATCH_POLL_INTERVAL,
                    max_wait=BATCH_MAX_WAIT,
                )
            except Exception as e:
                logger.warning(f"  Batch draft generation failed, falling back to sync: {e}")
                draft_results = {}

            # Write batch draft results
            for candidate in draft_candidates:
                db_id = candidate["db_id"]
                draft_body = draft_results.get(db_id)

                # Sync fallback for any that failed in batch
                if not draft_body:
                    draft_body = draft_generator.generate_draft(
                        candidate["email_data"], candidate["action_context"]
                    )

                if draft_body and draft_generator._validate_output(draft_body, candidate["email_data"]):
                    db.insert_draft(db_id, user_id, draft_body)
                    drafts_generated += 1
                    logger.info(f"  Draft generated for: {candidate['email_data'].get('subject', '?')[:60]}")

        db.update_pipeline_run(
            run_id, status="completed",
            emails_scanned=len(emails),
            emails_processed=emails_processed,
            drafts_generated=drafts_generated,
        )
        return emails_processed, drafts_generated

    except Exception as e:
        logger.exception(f"  User {user_id[:8]}...: pipeline error: {e}")
        for email in emails:
            try:
                db.update_email_status(email["id"], "error")
            except Exception:
                pass
        db.update_pipeline_run(run_id, status="failed", error_message=str(e)[:500])
        return 0, 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    logger.info("Worker starting (batch mode)")
    logger.info(f"Poll interval: {POLL_INTERVAL}s, Initial window: {INITIAL_WINDOW}s, Max window: {MAX_WINDOW}s")

    db = SupabaseWorkerClient()
    logger.info("Supabase client initialized")

    current_window = INITIAL_WINDOW

    while not _shutdown:
        # -- Onboarding check ------------------------------------------
        try:
            pending = db.get_users_needing_onboarding()
            for uid in pending:
                if _shutdown:
                    break
                profile = db.fetch_user_config(uid)
                logger.info(f"Running onboarding for user {uid[:8]}...")
                run_onboarding(db, uid, profile)
        except Exception as e:
            logger.error(f"Onboarding check error: {e}")

        # -- Async calibration check -----------------------------------
        try:
            cal_pending = db.get_users_needing_calibration()
            for uid in cal_pending:
                if _shutdown:
                    break
                logger.info(f"Running Opus calibration for user {uid[:8]}...")
                run_calibration(db, uid)
        except Exception as e:
            logger.error(f"Calibration check error: {e}")

        logger.info(f"--- Accumulation window: {current_window}s ---")

        try:
            accumulated = accumulate_emails(db, current_window)
        except Exception as e:
            logger.exception(f"Accumulation error: {e}")
            accumulated = {}

        # Check if any emails were found
        total_emails = sum(len(u["emails"]) for u in accumulated.values())

        if total_emails == 0:
            if not accumulated:
                # All users filtered out by activity check — deep sleep
                current_window = MAX_WINDOW
                logger.info(f"All users inactive, deep sleep: {current_window}s")
            else:
                # Active users exist but no new emails — normal backoff
                current_window = min(current_window * 2, MAX_WINDOW)
                logger.info(f"No emails found. Next window: {current_window}s")
            continue

        # Emails found — reset window to initial
        current_window = INITIAL_WINDOW
        logger.info(f"Found {total_emails} emails across {len(accumulated)} user(s)")

        # Process each user's batch
        total_processed = 0
        total_drafts = 0

        for user_id, data in accumulated.items():
            if _shutdown:
                break
            if not data["emails"]:
                continue

            logger.info(f"Processing user {user_id[:8]}...: {len(data['emails'])} emails")
            processed, drafts = process_user_batch(db, user_id, data["profile"], data["emails"])
            total_processed += processed
            total_drafts += drafts

        logger.info(f"Window complete: {total_processed} processed, {total_drafts} drafts")

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
