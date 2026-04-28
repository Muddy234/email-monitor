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
"""


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
