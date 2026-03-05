"""Railway pipeline worker — entry point.

Standalone Python process that polls Supabase for unprocessed emails,
runs them through the pipeline (filter → Claude → drafts), and writes
results back to Supabase.

Features:
- Atomic claim-and-process via RPC (prevents duplicate processing)
- Per-user round-robin (one batch per user per cycle)
- Graceful shutdown on SIGTERM (Railway sends this on deploy)
- Pipeline run logging per user per cycle
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime

from supabase_client import SupabaseWorkerClient
from run_pipeline import build_config_from_profile, process_email_batch

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

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_cycle(db: SupabaseWorkerClient):
    """Run one poll cycle: discover users, process one batch per user.

    Returns:
        int: Total emails processed across all users this cycle.
    """
    user_ids = db.get_users_with_unprocessed()
    if not user_ids:
        return 0

    logger.info(f"Found {len(user_ids)} user(s) with unprocessed emails")
    total_processed = 0

    for user_id in user_ids:
        if _shutdown:
            break

        # Fetch user profile for config
        profile = db.fetch_user_config(user_id)
        if not profile:
            logger.warning(f"No profile for user {user_id}, skipping")
            continue

        # Only process if user is actively logged in
        if not profile.get("worker_active", False):
            logger.info(f"User {user_id[:8]}...: worker_active=false, skipping")
            continue

        # Atomic claim
        claimed = db.claim_unprocessed_emails(user_id, limit=BATCH_SIZE)
        if not claimed:
            continue

        logger.info(f"User {user_id[:8]}...: claimed {len(claimed)} emails")

        # Create pipeline run log
        run_id = db.create_pipeline_run(user_id, trigger_type="scheduled")

        try:
            config = build_config_from_profile(profile)
            result = process_email_batch(db, claimed, user_id, config)

            db.update_pipeline_run(
                run_id,
                status="completed",
                emails_scanned=len(claimed),
                emails_processed=result["emails_processed"],
                drafts_generated=result["drafts_generated"],
            )

            total_processed += result["emails_processed"]
            logger.info(
                f"User {user_id[:8]}...: processed={result['emails_processed']}, "
                f"drafts={result['drafts_generated']}"
            )

        except Exception as e:
            logger.exception(f"User {user_id[:8]}...: pipeline error: {e}")

            # Mark claimed emails as error so they aren't stuck in 'processing'
            for email in claimed:
                try:
                    db.update_email_status(email["id"], "error")
                except Exception:
                    pass

            db.update_pipeline_run(
                run_id,
                status="failed",
                error_message=str(e)[:500],
            )

    return total_processed


def main():
    logger.info("Worker starting")
    logger.info(f"Poll interval: {POLL_INTERVAL}s, Batch size: {BATCH_SIZE}")

    db = SupabaseWorkerClient()
    logger.info("Supabase client initialized")

    while not _shutdown:
        try:
            processed = run_cycle(db)
            if processed > 0:
                logger.info(f"Cycle complete: {processed} emails processed")
        except Exception as e:
            logger.exception(f"Cycle error: {e}")

        # Sleep in small increments so SIGTERM is handled promptly
        for _ in range(POLL_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
