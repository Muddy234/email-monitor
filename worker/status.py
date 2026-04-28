"""Status vocabulary constants for the Email_Monitor worker.

Unified vocabulary (current state):
    pending  -> queued, not yet started
    active   -> in progress
    done     -> completed successfully
    failed   -> failed, retry permitted within budget
    skipped  -> intentionally not processed (noise, deferred, user action)

Phase 5 stripped the legacy classes, mapping tables, and dual-read helpers
that bridged the migration from the pre-unified vocabularies. The CHECK
constraints in migration 045 now reject any non-unified value at the DB.

LegacyOnboardingStatus is preserved — onboarding uses its own multi-stage
FSM that is intentionally separate from the email/draft/pipeline_run
status columns and is out of scope for this refactor.

Helpers:
    sanitize_error  — strip secrets/paths/multi-line traces before writing
                      last_error.
    transition_to   — single canonical writer for status transitions on
                      emails / drafts / pipeline_runs. Used by the reaper.
"""

import logging
import re

logger = logging.getLogger("worker")

# Tables this module knows how to transition. Onboarding (profiles) uses a
# separate FSM and goes through update_onboarding_status, not transition_to.
_TRANSITION_TABLES = ("emails", "drafts", "pipeline_runs")

_SECRET_KEY_RE = re.compile(
    r"(password|token|secret|api[-_]?key|bearer)[\s:=]+\S+", re.I
)
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s,)\]]*|/[\w./-]+\.py")


class Status:
    """Unified lifecycle vocabulary. Target state for every status column."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class LegacyOnboardingStatus:
    """Values written to profiles.onboarding_status.

    Onboarding uses a 12-state FSM that is intentionally separate from the
    unified email/draft/pipeline_run vocabulary and is out of scope for the
    Phase 1-5 refactor.
    """

    PENDING = "pending"
    STARTING = "starting"
    COLLECTING = "collecting"
    STATISTICS = "statistics"
    PERSISTING = "persisting"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    STYLE_GUIDE = "style_guide"
    TRAINING = "training"
    COMPLETE = "complete"
    COMPLETE_PARTIAL = "complete_partial"
    FAILED = "failed"


class DeliveryState:
    """drafts.delivery_state — separates Outlook/user state from generation state."""

    NOT_DELIVERED = "not_delivered"       # pending generation or pre-push
    DELIVERED = "delivered"               # in Outlook drafts folder
    USER_DELETED = "user_deleted"         # replaces draft_deleted boolean (Decision 1b)
    EDITED_IN_OUTLOOK = "edited_in_outlook"   # reserved: iteration 2+
    SENT = "sent"                         # reserved: iteration 2+
    STALE = "stale"                       # reserved: iteration 2+


class DeferredReason:
    """emails.deferred_reason — why a row landed in 'skipped' instead of 'done'."""

    ONBOARDING = "onboarding"
    USER_DISMISSED = "user_dismissed"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def sanitize_error(exc, max_len=500):
    """Strip paths, credentials, and multi-line traces from an error string.

    Use on every path that writes `last_error`. Keeps the column free of
    leaked secrets and tidy on the dashboard.

    Args:
        exc: Exception instance or string.
        max_len: Trim to this many characters after sanitization.
    """
    if isinstance(exc, str):
        msg = exc
    else:
        msg = f"{type(exc).__name__}: {exc}"
    # Keep only the first line — drop multi-line tracebacks.
    msg = msg.splitlines()[0] if msg else ""
    msg = _SECRET_KEY_RE.sub(r"\1=<redacted>", msg)
    msg = _PATH_RE.sub("<path>", msg)
    return msg[:max_len]


def transition_to(db, entity, entity_id, new_status, error=None):
    """Update a status-bearing row to `new_status`.

    Single canonical writer used by the reaper. Triggers in migration 038
    bump `state_entered_at` automatically when status changes.

    Args:
        db:         SupabaseWorkerClient instance.
        entity:     One of 'emails', 'drafts', 'pipeline_runs'.
        entity_id:  UUID of the row.
        new_status: Status.* value.
        error:      Optional message; sanitized and written to last_error
                    when transitioning to FAILED.
    """
    if entity not in _TRANSITION_TABLES:
        raise ValueError(f"transition_to: unsupported entity {entity!r}")

    payload = {"status": new_status}
    if new_status == Status.FAILED and error is not None:
        payload["last_error"] = sanitize_error(error)

    db.client.table(entity).update(payload).eq("id", entity_id).execute()
