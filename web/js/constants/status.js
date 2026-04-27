/**
 * Status vocabulary constants for the Email_Monitor dashboard.
 *
 * Mirrors worker/status.py and extension/status.js. Loaded as an ES module.
 *
 * Phase 1 of the state management refactor introduces this module. During
 * Phase 1 and 2, writers continue emitting legacy values but reference them
 * through the Legacy* objects so the eventual cutover is a single change
 * rather than a grep-and-replace across every call site.
 */

// Unified lifecycle vocabulary. Target state for every status column.
export const Status = Object.freeze({
  PENDING: "pending",
  ACTIVE: "active",
  DONE: "done",
  FAILED: "failed",
  SKIPPED: "skipped",
});

// Values actually written to emails.status today.
export const LegacyEmailStatus = Object.freeze({
  UNPROCESSED: "unprocessed",
  PROCESSING: "processing",
  PROCESSED: "processed",
  ONBOARDING: "onboarding",
  ERROR: "error",
  COMPLETED: "completed",
  DISMISSED: "dismissed",
});

// Values actually written to drafts.status today.
export const LegacyDraftStatus = Object.freeze({
  PENDING: "pending",
  WRITTEN: "written",
  DELETED: "deleted",
});

// Values actually written to pipeline_runs.status today.
export const LegacyPipelineRunStatus = Object.freeze({
  RUNNING: "running",
  COMPLETED: "completed",
  FAILED: "failed",
  PARTIAL_FAILURE: "partial_failure",
});

// Values actually written to profiles.onboarding_status today.
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

// ============================================================
// Legacy-to-new vocabulary mappings.
// ============================================================

export const EMAIL_LEGACY_TO_NEW = Object.freeze({
  [LegacyEmailStatus.UNPROCESSED]: Status.PENDING,
  [LegacyEmailStatus.PROCESSING]: Status.ACTIVE,
  [LegacyEmailStatus.PROCESSED]: Status.DONE,
  [LegacyEmailStatus.COMPLETED]: Status.DONE,
  [LegacyEmailStatus.ERROR]: Status.FAILED,
  [LegacyEmailStatus.ONBOARDING]: Status.SKIPPED,
  [LegacyEmailStatus.DISMISSED]: Status.SKIPPED,
});

export const DRAFT_LEGACY_TO_NEW = Object.freeze({
  [LegacyDraftStatus.PENDING]: Status.PENDING,
  [LegacyDraftStatus.WRITTEN]: Status.DONE,
  [LegacyDraftStatus.DELETED]: Status.SKIPPED,
});

export const PIPELINE_RUN_LEGACY_TO_NEW = Object.freeze({
  [LegacyPipelineRunStatus.RUNNING]: Status.ACTIVE,
  [LegacyPipelineRunStatus.COMPLETED]: Status.DONE,
  [LegacyPipelineRunStatus.FAILED]: Status.FAILED,
  [LegacyPipelineRunStatus.PARTIAL_FAILURE]: Status.DONE,
});

// ============================================================
// Dual-read helpers.
// ============================================================

export function emailPendingValues() {
  return [LegacyEmailStatus.UNPROCESSED, Status.PENDING];
}

export function emailActiveValues() {
  return [LegacyEmailStatus.PROCESSING, Status.ACTIVE];
}

export function emailDoneValues() {
  return [
    LegacyEmailStatus.PROCESSED,
    LegacyEmailStatus.COMPLETED,
    Status.DONE,
  ];
}

export function draftPendingValues() {
  return [LegacyDraftStatus.PENDING, Status.PENDING];
}

export function draftDeliveredValues() {
  return [LegacyDraftStatus.WRITTEN, Status.DONE];
}

export function pipelineRunActiveValues() {
  return [LegacyPipelineRunStatus.RUNNING, Status.ACTIVE];
}

// Pipeline-run terminal-success values. partial_failure is NOT listed because
// the unified vocabulary folds it into done+has_partial_failures=true; callers
// that care about partial must check the boolean separately.
export function pipelineRunDoneValues() {
  return [LegacyPipelineRunStatus.COMPLETED, Status.DONE];
}

// Emails a user has resolved from the inbox view — legacy completed/dismissed
// plus their new-vocabulary equivalents (done / skipped-with-user_dismissed).
// Over-matches skipped-for-onboarding emails by one status value; callers that
// need to distinguish must check deferred_reason.
export function emailResolvedValues() {
  return [
    LegacyEmailStatus.COMPLETED,
    LegacyEmailStatus.DISMISSED,
    Status.DONE,
    Status.SKIPPED,
  ];
}

// Values used to exclude tombstoned drafts from inbox joins.
// Legacy 'deleted' and its new-vocabulary equivalent 'skipped'.
export function draftExcludedValues() {
  return [LegacyDraftStatus.DELETED, Status.SKIPPED];
}
