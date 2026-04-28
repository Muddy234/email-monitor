"""Reaper smoke test — verifies migrations 046/047/048 + worker/reaper.py.

Run from project root:
    python worker/reaper_smoke.py

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from .env or environment.

What it covers:
  1. system_config: table exists, both seed keys present.
  2. find_stuck_entities: RPC callable, returns rows or empty list.
  3. last_error column-level revoke: SELECT permission for anon/auth gone.
  4. sanitize_error: secrets and paths get redacted.
  5. reaper retry path: stuck row with failure_count<3 -> status flips
     back to 'pending'.
  6. reaper fail path: stuck row with failure_count>=3 -> status flips
     to 'failed' and last_error populated.
  7. kill switch: reaper_enabled=false short-circuits the tick.

Test rows are inserted with email_ref='reaper_smoke_<ts>_<n>' and deleted
after the test. If any step fails, the script attempts cleanup before
exiting non-zero.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
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

# Make the worker/ package importable as flat modules
sys.path.insert(0, str(Path(__file__).resolve().parent))

from supabase import create_client  # noqa: E402

from supabase_client import SupabaseWorkerClient  # noqa: E402
from status import Status, sanitize_error  # noqa: E402
from reaper import run_reaper, RETRY_BUDGET  # noqa: E402
from config import get_config, _cache as config_cache  # noqa: E402

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
sb = create_client(url, key)
db = SupabaseWorkerClient()

# Track test rows for cleanup
_test_email_ids: list[str] = []


def banner(msg: str) -> None:
    print(f"\n=== {msg} ===")


def ok(msg: str) -> None:
    print(f"  OK: {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL: {msg}")


def skip(msg: str) -> None:
    print(f"  SKIP: {msg}")


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _pick_user_id() -> str | None:
    r = sb.table("emails").select("user_id").limit(1).execute()
    if not r.data:
        return None
    return r.data[0]["user_id"]


def _insert_stuck_email(user_id: str, failure_count: int, age_minutes: int = 11) -> str | None:
    """Insert a stuck email row. Returns the row id, or None on failure."""
    aged = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
    suffix = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    row = {
        "user_id": user_id,
        "email_ref": f"reaper_smoke_{suffix}",
        "subject": "[reaper_smoke_test] do not process",
        "sender": "smoke@test.local",
        "status": Status.ACTIVE,
        "state_entered_at": aged,
        "failure_count": failure_count,
        "received_time": aged,
        "folder": "Inbox",
    }
    try:
        ins = sb.table("emails").insert(row).execute()
        if ins.data:
            row_id = ins.data[0]["id"]
            _test_email_ids.append(row_id)
            return row_id
    except Exception as exc:
        print(f"     insert error: {exc}")
    return None


def _cleanup() -> None:
    if not _test_email_ids:
        return
    try:
        sb.table("emails").delete().in_("id", _test_email_ids).execute()
        print(f"\nCleanup: deleted {len(_test_email_ids)} test row(s)")
    except Exception as exc:
        print(f"\nCleanup error: {exc}")


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


def test_system_config() -> bool:
    banner("system_config table + seeded keys (migration 046)")
    try:
        r = sb.table("system_config").select("key, value").execute()
    except Exception as exc:
        fail(f"system_config query failed: {exc}")
        return False
    keys = {row["key"]: row["value"] for row in (r.data or [])}
    if "reaper_enabled" not in keys:
        fail("seed key 'reaper_enabled' missing")
        return False
    if "reaper_paused_until" not in keys:
        fail("seed key 'reaper_paused_until' missing")
        return False
    ok(f"reaper_enabled={keys['reaper_enabled']!r}")
    ok(f"reaper_paused_until={keys['reaper_paused_until']!r}")
    return True


def test_find_stuck_entities_rpc() -> bool:
    banner("find_stuck_entities RPC callable (migration 047)")
    try:
        r = sb.rpc("find_stuck_entities").execute()
    except Exception as exc:
        fail(f"RPC error: {exc}")
        return False
    rows = r.data or []
    ok(f"RPC returned {len(rows)} stuck row(s)")
    # Loose schema check on whatever came back
    for row in rows[:3]:
        for col in ("entity", "id", "failure_count", "state_entered_at"):
            if col not in row:
                fail(f"row missing '{col}': {row}")
                return False
    return True


def test_sanitize_error() -> bool:
    banner("sanitize_error helper (worker/status.py)")
    cases = [
        ("password=hunter2 leaked", "password=<redacted>"),
        ("api_key: sk-abc123 in body", "api_key=<redacted>"),
        ("Bearer eyJhbGciOiJIUzI1NiJ9", "Bearer=<redacted>"),
        ("ValueError at C:\\foo\\bar.py:42", "<path>"),
        ("file /home/user/foo.py crashed", "<path>"),
    ]
    failed = 0
    for inp, expect_substring in cases:
        out = sanitize_error(inp)
        if expect_substring.lower() in out.lower():
            ok(f"{inp!r} -> {out!r}")
        else:
            fail(f"{inp!r} did not produce {expect_substring!r}: got {out!r}")
            failed += 1
    # Multi-line stripping
    multi = sanitize_error("first line\nsecond line\nthird line")
    if "second line" in multi or "third line" in multi:
        fail(f"multi-line stripping failed: {multi!r}")
        failed += 1
    else:
        ok(f"multi-line stripped: {multi!r}")
    return failed == 0


def test_reaper_retry_path() -> bool:
    banner("reaper retry path (failure_count < budget -> pending)")
    user_id = _pick_user_id()
    if not user_id:
        skip("no user_id available")
        return True
    row_id = _insert_stuck_email(user_id, failure_count=0, age_minutes=11)
    if not row_id:
        fail("could not insert stuck email")
        return False
    ok(f"inserted stuck email {row_id[:8]}... (failure_count=0)")

    config_cache.clear()  # ensure reaper_enabled() reads fresh
    run_reaper(db)

    after = sb.table("emails").select("status, last_error").eq("id", row_id).single().execute()
    status_after = after.data.get("status")
    if status_after != Status.PENDING:
        fail(f"expected status='pending', got {status_after!r}")
        return False
    ok(f"row reset to pending")
    return True


def test_reaper_fail_path() -> bool:
    banner(f"reaper fail path (failure_count >= {RETRY_BUDGET} -> failed)")
    user_id = _pick_user_id()
    if not user_id:
        skip("no user_id available")
        return True
    row_id = _insert_stuck_email(user_id, failure_count=RETRY_BUDGET, age_minutes=11)
    if not row_id:
        fail("could not insert stuck email")
        return False
    ok(f"inserted stuck email {row_id[:8]}... (failure_count={RETRY_BUDGET})")

    config_cache.clear()
    run_reaper(db)

    after = sb.table("emails").select("status, last_error").eq("id", row_id).single().execute()
    status_after = after.data.get("status")
    last_error_after = after.data.get("last_error")
    if status_after != Status.FAILED:
        fail(f"expected status='failed', got {status_after!r}")
        return False
    ok(f"row marked failed")
    if not last_error_after or "retry budget" not in last_error_after.lower():
        fail(f"last_error missing or unexpected: {last_error_after!r}")
        return False
    ok(f"last_error={last_error_after!r}")
    return True


def test_kill_switch() -> bool:
    banner("kill switch (reaper_enabled=false)")
    user_id = _pick_user_id()
    if not user_id:
        skip("no user_id available")
        return True
    row_id = _insert_stuck_email(user_id, failure_count=0, age_minutes=11)
    if not row_id:
        fail("could not insert stuck email")
        return False

    # Disable
    sb.table("system_config").update({"value": False}).eq("key", "reaper_enabled").execute()
    config_cache.clear()
    enabled_now = get_config(db, "reaper_enabled")
    if enabled_now is not False:
        fail(f"expected reaper_enabled=False after toggle; got {enabled_now!r}")
        # Re-enable before bailing
        sb.table("system_config").update({"value": True}).eq("key", "reaper_enabled").execute()
        return False

    run_reaper(db)

    after = sb.table("emails").select("status").eq("id", row_id).single().execute()
    status_after = after.data.get("status")

    # Re-enable so subsequent runs work
    sb.table("system_config").update({"value": True}).eq("key", "reaper_enabled").execute()
    config_cache.clear()

    if status_after != Status.ACTIVE:
        fail(f"reaper ran while disabled — row was modified to {status_after!r}")
        return False
    ok("reaper short-circuited; row left untouched")
    return True


def test_last_error_rls() -> bool:
    banner("last_error column revoked from anon/authenticated (migration 048)")
    anon_key = os.environ.get("SUPABASE_ANON_KEY")
    if not anon_key:
        skip("SUPABASE_ANON_KEY not set; cannot exercise anon-role read")
        return True
    anon = create_client(url, anon_key)
    try:
        anon.table("emails").select("last_error").limit(1).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if "permission" in msg or "denied" in msg or "42501" in msg:
            ok(f"anon SELECT on last_error denied: {exc}")
            return True
        fail(f"anon SELECT failed but not with a permission error: {exc}")
        return False
    # If we got here, the SELECT was accepted — revoke didn't take effect.
    fail("anon SELECT(last_error) on emails was accepted; revoke not in effect")
    return False


# -----------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------


def main() -> int:
    print("Reaper smoke test")
    print(f"Supabase URL: {url}")

    results: list[tuple[str, bool]] = []
    try:
        results.append(("system_config", test_system_config()))
        results.append(("find_stuck_entities RPC", test_find_stuck_entities_rpc()))
        results.append(("last_error RLS", test_last_error_rls()))
        results.append(("sanitize_error", test_sanitize_error()))
        results.append(("reaper retry path", test_reaper_retry_path()))
        results.append(("reaper fail path", test_reaper_fail_path()))
        results.append(("kill switch", test_kill_switch()))
    finally:
        _cleanup()

    print("\n--- Summary ---")
    for name, passed in results:
        marker = "PASS" if passed else "FAIL"
        print(f"  [{marker}] {name}")

    failed = [n for n, p in results if not p]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
