/**
 * Status vocabulary constants for the Email_Monitor dashboard.
 *
 * Mirrors worker/status.py and extension/status.js. Loaded as an ES module.
 *
 * Phase 5 stripped the legacy classes, mapping tables, and dual-read helpers
 * that bridged the migration from the pre-unified vocabularies. Migration
 * 045 added CHECK constraints so the DB now rejects non-unified values.
 *
 * LegacyOnboardingStatus is preserved — onboarding uses its own multi-stage
 * FSM that is intentionally separate from email/draft/pipeline_run status.
 */

// Unified lifecycle vocabulary. Target state for every status column.
export const Status = Object.freeze({
  PENDING: "pending",
  ACTIVE: "active",
  DONE: "done",
  FAILED: "failed",
  SKIPPED: "skipped",
});

// Values written to profiles.onboarding_status.
export const LegacyOnboardingStatus = Object.freeze({
  PENDING: "pending",
  STARTING: "starting",
  COLLECTING: "collecting",
  STATISTICS: "statistics",
  PERSISTING: "persisting",
  EXTRACTING: "extracting",
  SYNTHESIZING: "synthesizing",
  STYLE_GUIDE: "style_guide",
  TRAINING: "training",
  COMPLETE: "complete",
  COMPLETE_PARTIAL: "complete_partial",
  FAILED: "failed",
});

// drafts.delivery_state — separates Outlook/user state from generation state.
export const DeliveryState = Object.freeze({
  NOT_DELIVERED: "not_delivered",
  DELIVERED: "delivered",
  USER_DELETED: "user_deleted",
  EDITED_IN_OUTLOOK: "edited_in_outlook",
  SENT: "sent",
  STALE: "stale",
});

// emails.deferred_reason — why a row landed in 'skipped' instead of 'done'.
export const DeferredReason = Object.freeze({
  ONBOARDING: "onboarding",
  USER_DISMISSED: "user_dismissed",
});
