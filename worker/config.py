"""System config reader with TTL cache.

Backed by the public.system_config table (migration 046). Service-role only.
Used by the reaper to consult its kill switch without hammering the DB.
"""

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("worker")

_cache = {}
_CACHE_TTL_SECONDS = 60


def get_config(db, key, default=None):
    """Read a system_config value with 60s TTL.

    Changes to system_config take up to 60s to propagate to the worker.
    Trade-off for not querying the DB on every reaper tick.
    """
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL_SECONDS:
        return cached["value"]

    try:
        row = (
            db.client.table("system_config")
            .select("value")
            .eq("key", key)
            .maybe_single()
            .execute()
        )
        value = row.data["value"] if row and row.data else default
    except Exception as exc:
        logger.warning(f"system_config read failed for {key!r}: {exc}")
        value = default

    _cache[key] = {"value": value, "fetched_at": now}
    return value


def reaper_enabled(db) -> bool:
    """True if the reaper should run on this tick.

    Honors both `reaper_enabled` (master switch) and `reaper_paused_until`
    (auto-resuming pause). Defaults to enabled if either key is missing.
    """
    if get_config(db, "reaper_enabled", True) is not True:
        return False

    paused_until = get_config(db, "reaper_paused_until")
    if paused_until:
        try:
            until = datetime.fromisoformat(str(paused_until).replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < until:
                return False
        except (ValueError, TypeError):
            logger.warning(f"reaper_paused_until is not a valid ISO timestamp: {paused_until!r}")

    return True
