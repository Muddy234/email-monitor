"""Reaper — autonomous self-healing for stuck rows.

Runs once at the end of every worker cycle (locked, iteration 1). Calls
the `find_stuck_entities` RPC (migration 047) to enumerate rows in
`status='active'` that have exceeded their per-entity timeout, then
either retries them (status -> pending) or fails them (status -> failed)
based on the per-row failure_count.

Iteration 1 scope: emails, drafts, pipeline_runs. Onboarding (profiles)
keeps `_recover_stuck_onboarding` until iteration 2.
"""

import logging

from config import reaper_enabled
from status import Status, transition_to

logger = logging.getLogger("worker")

RETRY_BUDGET = 3


def run_reaper(db, shutdown_probe=None):
    """Single reaper tick.

    Args:
        db:             SupabaseWorkerClient instance.
        shutdown_probe: Optional zero-arg callable returning True if the
                        worker is shutting down. Checked between rows so
                        SIGTERM exits within seconds rather than blocking
                        on the full sweep.
    """
    if not reaper_enabled(db):
        logger.debug("reaper: disabled via system_config; skipping tick")
        return

    try:
        result = db.client.rpc("find_stuck_entities").execute()
    except Exception as exc:
        logger.error(f"reaper: find_stuck_entities RPC failed: {exc}")
        return

    rows = result.data or []
    if not rows:
        logger.debug("reaper: no stuck rows")
        return

    counts = {"retried": 0, "failed": 0, "errors": 0}
    by_entity = {}

    for row in rows:
        if shutdown_probe is not None and shutdown_probe():
            logger.info(
                f"reaper: shutdown signaled mid-tick "
                f"(processed {sum(counts.values())} of {len(rows)})"
            )
            break

        entity = row.get("entity")
        entity_id = row.get("id")
        failure_count = row.get("failure_count") or 0

        if not entity or not entity_id:
            counts["errors"] += 1
            continue

        try:
            if failure_count >= RETRY_BUDGET:
                transition_to(
                    db,
                    entity,
                    entity_id,
                    Status.FAILED,
                    error=f"Exceeded retry budget ({RETRY_BUDGET})",
                )
                counts["failed"] += 1
                by_entity.setdefault(entity, {"retried": 0, "failed": 0})["failed"] += 1
            else:
                transition_to(db, entity, entity_id, Status.PENDING)
                counts["retried"] += 1
                by_entity.setdefault(entity, {"retried": 0, "failed": 0})["retried"] += 1
        except Exception as exc:
            counts["errors"] += 1
            logger.error(
                f"reaper: transition failed for {entity}/{str(entity_id)[:8]}...: {exc}"
            )

    summary = ", ".join(
        f"{e}: retried={v['retried']} failed={v['failed']}"
        for e, v in sorted(by_entity.items())
    )
    logger.info(
        f"reaper: tick complete — "
        f"retried={counts['retried']} failed={counts['failed']} "
        f"errors={counts['errors']} ({summary or 'no actions'})"
    )
