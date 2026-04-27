"""Phase 4 validation — verify writers are emitting unified vocabulary.

Run from project root:
    python worker/phase4_validate.py

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from .env or environment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env from project root
root = Path(__file__).resolve().parent.parent
env_file = root / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from supabase import create_client  # noqa: E402

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
sb = create_client(url, key)


def banner(msg: str) -> None:
    print(f"\n=== {msg} ===")


def check_column_exists() -> None:
    """Migration 042: drafts.written_by_extension_version must exist."""
    banner("1. drafts.written_by_extension_version column")
    res = sb.rpc(
        "exec_sql",
        {
            "sql": """
              select column_name, data_type, is_nullable
              from information_schema.columns
              where table_schema='public'
                and table_name='drafts'
                and column_name='written_by_extension_version'
            """
        },
    ).execute() if False else None  # rpc may not exist; fallback to a query
    # Use information_schema via select on columns is not directly exposed;
    # easier path — just attempt to select the column on 1 row.
    try:
        r = (
            sb.table("drafts")
            .select("id, written_by_extension_version")
            .limit(1)
            .execute()
        )
        print(f"  OK: column readable. sample row(s): {len(r.data)}")
    except Exception as e:
        print(f"  FAIL: {e}")


def recent_pipeline_run_statuses(minutes: int = 60) -> None:
    banner(f"2. pipeline_runs status distribution (last {minutes}m)")
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()
    r = (
        sb.table("pipeline_runs")
        .select("status, has_partial_failures, started_at")
        .gte("started_at", cutoff)
        .order("started_at", desc=True)
        .limit(100)
        .execute()
    )
    counts: dict[str, int] = {}
    pf_count = 0
    for row in r.data:
        s = row.get("status") or "<null>"
        counts[s] = counts.get(s, 0) + 1
        if row.get("has_partial_failures"):
            pf_count += 1
    print(f"  total runs (last 24h): {len(r.data)}")
    for s, c in sorted(counts.items()):
        print(f"    {s}: {c}")
    print(f"  rows with has_partial_failures=true: {pf_count}")
    legacy = {"running", "completed", "partial_failure"}
    bad = [s for s in counts if s in legacy]
    if bad:
        print(f"  WARN: legacy statuses still present: {bad}")
    else:
        print("  OK: no legacy pipeline_run statuses")


def recent_email_statuses(minutes: int = 60) -> None:
    banner(f"3. emails status distribution (last {minutes}m)")
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()
    r = (
        sb.table("emails")
        .select("status, deferred_reason, state_entered_at")
        .gte("state_entered_at", cutoff)
        .order("state_entered_at", desc=True)
        .limit(500)
        .execute()
    )
    counts: dict[str, int] = {}
    deferred_counts: dict[str, int] = {}
    for row in r.data:
        s = row.get("status") or "<null>"
        counts[s] = counts.get(s, 0) + 1
        d = row.get("deferred_reason")
        if d:
            deferred_counts[d] = deferred_counts.get(d, 0) + 1
    print(f"  total updated (last 24h): {len(r.data)}")
    for s, c in sorted(counts.items()):
        print(f"    status={s}: {c}")
    if deferred_counts:
        print("  deferred_reason distribution:")
        for d, c in sorted(deferred_counts.items()):
            print(f"    {d}: {c}")
    legacy = {"unprocessed", "processing", "processed", "onboarding", "error"}
    bad = [s for s in counts if s in legacy]
    if bad:
        print(f"  WARN: legacy email statuses written recently: {bad}")
    else:
        print("  OK: no legacy email statuses")


def recent_draft_statuses(minutes: int = 60) -> None:
    banner(
        f"4. drafts status + delivery_state + version (last {minutes}m)"
    )
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()
    r = (
        sb.table("drafts")
        .select(
            "status, delivery_state, written_by_extension_version, "
            "state_entered_at"
        )
        .gte("state_entered_at", cutoff)
        .order("state_entered_at", desc=True)
        .limit(500)
        .execute()
    )
    status_counts: dict[str, int] = {}
    delivery_counts: dict[str, int] = {}
    null_delivery = 0
    version_counts: dict[str, int] = {}
    null_version = 0
    for row in r.data:
        s = row.get("status") or "<null>"
        status_counts[s] = status_counts.get(s, 0) + 1
        ds = row.get("delivery_state")
        if ds is None:
            null_delivery += 1
        else:
            delivery_counts[ds] = delivery_counts.get(ds, 0) + 1
        v = row.get("written_by_extension_version")
        if v is None:
            null_version += 1
        else:
            version_counts[v] = version_counts.get(v, 0) + 1
    print(f"  total drafts updated (last 24h): {len(r.data)}")
    print("  status:")
    for s, c in sorted(status_counts.items()):
        print(f"    {s}: {c}")
    print("  delivery_state:")
    for d, c in sorted(delivery_counts.items()):
        print(f"    {d}: {c}")
    print(f"    NULL: {null_delivery}")
    print("  written_by_extension_version:")
    for v, c in sorted(version_counts.items()):
        print(f"    {v}: {c}")
    print(f"    NULL (legacy writers): {null_version}")
    legacy = {"completed", "dismissed", "written", "deleted"}
    bad = [s for s in status_counts if s in legacy]
    if bad:
        print(f"  WARN: legacy draft statuses written recently: {bad}")
    else:
        print("  OK: no legacy draft statuses")


def main() -> int:
    check_column_exists()
    recent_pipeline_run_statuses()
    recent_email_statuses()
    recent_draft_statuses()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
