"""Status vocabulary constants for the Email_Monitor worker.

Phase 1 of the state management refactor introduces this module. During
Phase 1 and 2, writers continue emitting legacy values but reference them
through the Legacy* classes so the eventual cutover is a single import
swap rather than a grep-and-replace exercise across every call site.

Unified vocabulary (target state):
    pending  -> queued, not yet started
    active   -> in progress
    done     -> completed successfully
    failed   -> failed, retry permitted within budget
    skipped  -> intentionally not processed (noise, deferred, user action)

Legacy classes capture values actually observed in production today
(verified via grep against worker/, extension/, web/). Values that were
defined but never written (e.g. drafts.status = 'sent'/'edited'/'obsolete',
emails.status = 'failed', profiles.onboarding_status = 'running') are
deliberately absent.
"""


class Status:
    """Unified lifecycle vocabulary. Target state for every status column."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class LegacyEmailStatus:
    """Values actually written to emails.status today."""

    UNPROCESSED = "unprocessed"   # worker: newly ingested
    PROCESSING = "processing"     # worker: claimed by RPC
    PROCESSED = "processed"       # worker: pipeline success
    ONBOARDING = "onboarding"     # worker: deferred during onboarding sweep
    ERROR = "error"               # worker: pipeline failure
    COMPLETED = "completed"       # dashboard user action
    DISMISSED = "dismissed"       # dashboard user action
    # NOTE: 'failed' is defined in many enumerations but never written
    # by any current code path on the emails table.


class LegacyDraftStatus:
    """Values actually written to drafts.status today."""

    PENDING = "pending"   # worker: created, awaiting extension
    WRITTEN = "written"   # extension: pushed to Outlook successfully
    DELETED = "deleted"   # extension: user deleted (always with draft_deleted=true)
    # NOTE: 'sent', 'edited', 'obsolete', 'failed' are NOT written today.


class LegacyPipelineRunStatus:
    """Values actually written to pipeline_runs.status today."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL_FAILURE = "partial_failure"


class LegacyOnboardingStatus:
    """Values actually written to profiles.onboarding_status today.

    Retained as legacy-only for iteration 1; collapse into the unified
    vocabulary is deferred to iteration 2+ (scope option C).
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


# ============================================================
# Legacy-to-new vocabulary mappings.
# Used by the Phase 3 migration and by dual-read helpers during Phase 2.
# ============================================================

EMAIL_LEGACY_TO_NEW = {
    LegacyEmailStatus.UNPROCESSED: Status.PENDING,
    LegacyEmailStatus.PROCESSING: Status.ACTIVE,
    LegacyEmailStatus.PROCESSED: Status.DONE,
    LegacyEmailStatus.COMPLETED: Status.DONE,        # dashboard user "mark complete"
    LegacyEmailStatus.ERROR: Status.FAILED,
    LegacyEmailStatus.ONBOARDING: Status.SKIPPED,    # + deferred_reason='onboarding'
    LegacyEmailStatus.DISMISSED: Status.SKIPPED,     # + deferred_reason='user_dismissed'
}

DRAFT_LEGACY_TO_NEW = {
    LegacyDraftStatus.PENDING: Status.PENDING,
    LegacyDraftStatus.WRITTEN: Status.DONE,          # + delivery_state='delivered'
    LegacyDraftStatus.DELETED: Status.SKIPPED,       # + delivery_state='user_deleted'
}

PIPELINE_RUN_LEGACY_TO_NEW = {
    LegacyPipelineRunStatus.RUNNING: Status.ACTIVE,
    LegacyPipelineRunStatus.COMPLETED: Status.DONE,
    LegacyPipelineRunStatus.FAILED: Status.FAILED,
    LegacyPipelineRunStatus.PARTIAL_FAILURE: Status.DONE,   # + has_partial_failures=true
}


# ============================================================
# Dual-read helpers.
# Return the tuple of accepted values during Phase 2, so filter queries
# can match both legacy and new vocabulary without branching on phase.
# ============================================================

def email_pending_values() -> tuple[str, ...]:
    """Values that mean "queued, not yet claimed" for emails."""
    return (LegacyEmailStatus.UNPROCESSED, Status.PENDING)


def email_active_values() -> tuple[str, ...]:
    """Values that mean "currently being processed" for emails."""
    return (LegacyEmailStatus.PROCESSING, Status.ACTIVE)


def email_done_values() -> tuple[str, ...]:
    """Values that mean "terminal success" for emails."""
    return (
        LegacyEmailStatus.PROCESSED,
        LegacyEmailStatus.COMPLETED,
        Status.DONE,
    )


def draft_pending_values() -> tuple[str, ...]:
    """Values that mean "awaiting delivery" for drafts."""
    return (LegacyDraftStatus.PENDING, Status.PENDING)


def draft_delivered_values() -> tuple[str, ...]:
    """Values that mean "successfully delivered to Outlook" for drafts."""
    return (LegacyDraftStatus.WRITTEN, Status.DONE)


def pipeline_run_active_values() -> tuple[str, ...]:
    """Values that mean "currently executing" for pipeline_runs."""
    return (LegacyPipelineRunStatus.RUNNING, Status.ACTIVE)
