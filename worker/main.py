"""Railway pipeline worker — entry point.

Accumulates emails over an adaptive time window, then processes them
in batch via the Anthropic Message Batches API (50% cost discount).

Adaptive window logic:
- Starts at INITIAL_WINDOW (45 seconds).
- If no emails arrive during a window, the next window doubles in length.
- Caps at MAX_WINDOW (6 minutes).
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
from run_pipeline import process_user_batch_signals
from onboarding import run_onboarding
from onboarding.model_trainer import check_retrain_needed, train_user_model

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
INITIAL_WINDOW = int(os.environ.get("INITIAL_WINDOW_SECONDS", "45"))  # 45s
MAX_WINDOW = int(os.environ.get("MAX_WINDOW_SECONDS", "180"))  # 3 minutes

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
        tuple: (dict {user_id: {"profile": profile, "emails": [rows]}},
                bool onboarding_needed — True if loop broke early for onboarding)
    """
    accumulated = {}  # user_id → {"profile": ..., "emails": [...]}
    deadline = time.time() + window_seconds
    poll_count = 0
    found_any = False

    while time.time() < deadline and not _shutdown:
        poll_count += 1

        # Check for pending onboarding — break early so main loop handles it
        try:
            pending = db.get_users_needing_onboarding()
            if pending:
                logger.info(f"  Onboarding needed for {len(pending)} user(s) — breaking accumulation")
                return accumulated, True
        except Exception:
            pass

        user_ids = db.get_users_with_unprocessed()

        for user_id in user_ids:
            if _shutdown:
                break

            # Get or cache profile
            if user_id not in accumulated:
                profile = db.fetch_user_config(user_id)
                if not profile or not _is_user_active(profile):
                    continue
                # Don't process emails until onboarding is complete
                if not profile.get("onboarding_completed_at"):
                    continue
                accumulated[user_id] = {"profile": profile, "emails": []}

            claimed = db.claim_unprocessed_emails(user_id, limit=BATCH_SIZE)
            if claimed:
                accumulated[user_id]["emails"].extend(claimed)
                logger.info(
                    f"  Poll {poll_count}: claimed {len(claimed)} for user {user_id[:8]}... "
                    f"(total: {len(accumulated[user_id]['emails'])})"
                )

                # Shorten window on first email arrival during long windows
                if not found_any and window_seconds > INITIAL_WINDOW:
                    new_deadline = time.time() + INITIAL_WINDOW
                    if new_deadline < deadline:
                        deadline = new_deadline
                        logger.info(
                            f"  Emails arrived during backoff — "
                            f"shortening window to {INITIAL_WINDOW}s"
                        )
                found_any = True

        # Sleep in 1s increments for responsive shutdown
        for _ in range(POLL_INTERVAL):
            if _shutdown or time.time() >= deadline:
                break
            time.sleep(1)

    return accumulated, False


# ---------------------------------------------------------------------------
# Batch processing phase
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _recover_stuck_onboarding(db):
    """Reset any users whose onboarding was interrupted mid-stage.

    On deploy/restart, a user may be stuck in a transient status like
    'collecting', 'extracting', etc.  Reset them to 'pending' so
    onboarding restarts cleanly on the next loop.
    """
    TERMINAL = {"complete", "complete_partial", "pending", "failed"}
    result = (
        db.client.table("profiles")
        .select("id, onboarding_status")
        .is_("onboarding_completed_at", "null")
        .execute()
    )
    for row in (result.data or []):
        status = row.get("onboarding_status")
        if status and status not in TERMINAL:
            uid = row["id"]
            logger.warning(
                f"Recovering stuck onboarding for {uid[:8]}... "
                f"(was '{status}') → resetting to 'pending'"
            )
            db.update_onboarding_status(uid, "pending")


def main():
    logger.info("Worker starting (batch mode)")
    logger.info(f"Poll interval: {POLL_INTERVAL}s, Initial window: {INITIAL_WINDOW}s, Max window: {MAX_WINDOW}s")

    db = SupabaseWorkerClient()
    logger.info("Supabase client initialized")

    # Recover any onboarding interrupted by a previous crash/redeploy
    try:
        _recover_stuck_onboarding(db)
    except Exception as e:
        logger.error(f"Stuck onboarding recovery error: {e}")

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

        # -- Model re-training check -----------------------------------
        try:
            for uid in db.get_active_user_ids():
                if _shutdown:
                    break
                if check_retrain_needed(db, uid):
                    logger.info(f"Re-training model for user {uid[:8]}...")
                    train_user_model(db, uid)
        except Exception as e:
            logger.error(f"Model re-training check error: {e}")

        # -- Recover stuck emails --------------------------------------
        try:
            reset_count = db.reset_stuck_processing()
            if reset_count:
                logger.warning(f"Recovered {reset_count} stuck processing email(s)")
        except Exception as e:
            logger.error(f"Stuck-email recovery error: {e}")

        logger.info(f"--- Accumulation window: {current_window}s ---")

        try:
            accumulated, onboarding_needed = accumulate_emails(db, current_window)
        except Exception as e:
            logger.exception(f"Accumulation error: {e}")
            accumulated = {}
            onboarding_needed = False

        # If accumulation broke early for onboarding, skip straight to next loop
        if onboarding_needed:
            current_window = INITIAL_WINDOW
            continue

        # Check if any emails were found
        total_emails = sum(len(u["emails"]) for u in accumulated.values())

        if total_emails == 0:
            # Check if any active users exist (don't use empty accumulated
            # as proxy — it just means no unprocessed emails at poll time)
            try:
                active_ids = db.get_active_user_ids()
                has_active = any(
                    _is_user_active(db.fetch_user_config(uid))
                    for uid in active_ids
                )
            except Exception:
                has_active = True  # err on the side of staying awake

            if not has_active:
                current_window = MAX_WINDOW
                logger.info(f"All users inactive, deep sleep: {current_window}s")
            else:
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

            if not db.is_subscription_active(user_id):
                logger.info(f"Skipping user {user_id[:8]}...: no active subscription")
                continue

            logger.info(f"Processing user {user_id[:8]}...: {len(data['emails'])} emails")
            processed, drafts = process_user_batch_signals(
                db, user_id, data["profile"], data["emails"]
            )
            total_processed += processed
            total_drafts += drafts

        logger.info(f"Window complete: {total_processed} processed, {total_drafts} drafts")

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
