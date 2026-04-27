/**
 * Status vocabulary constants for the Email_Monitor Chrome extension.
 *
 * Mirrors worker/status.py. Loaded as a classic (non-module) global script
 * so it works in both the MV3 service worker (via importScripts) and the
 * popup (via <script> tag).
 *
 * Phase 1 of the state management refactor introduces this module. During
 * Phase 1 and 2, writers continue emitting legacy values but reference them
 * through the Legacy* objects so the eventual cutover is a single change
 * rather than a grep-and-replace across every call site.
 */

// Unified lifecycle vocabulary. Target state for every status column.
const Status = Object.freeze({
  PENDING: "pending",
  ACTIVE: "active",
  DONE: "done",
  FAILED: "failed",
  SKIPPED: "skipped",
});

// Values actually written to emails.status today.
const LegacyEmailStatus = Object.freeze({
  UNPROCESSED: "unprocessed",   // worker: newly ingested
  PROCESSING: "processing",     // worker: claimed by RPC
  PROCESSED: "processed",       // worker: pipeline success
  ONBOARDING: "onboarding",     // worker: deferred during onboarding sweep
  ERROR: "error",               // worker: pipeline failure
  COMPLETED: "completed",       // dashboard user action
  DISMISSED: "dismissed",       // dashboard user action
});

// Values actually written to drafts.status today.
const LegacyDraftStatus = Object.freeze({
  PENDING: "pending",   // worker: created, awaiting extension
  WRITTEN: "written",   // extension: pushed to Outlook successfully
  DELETED: "deleted",   // extension: user deleted (always with draft_deleted=true)
});

// Values actually written to pipeline_runs.status today.
const LegacyPipelineRunStatus = Object.freeze({
  RUNNING: "running",
  COMPLETED: "completed",
  FAILED: "failed",
  PARTIAL_FAILURE: "partial_failure",
});

// Values actually written to profiles.onboarding_status today.
const LegacyOnboardingStatus = Object.freeze({
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
const DeliveryState = Object.freeze({
  NOT_DELIVERED: "not_delivered",       // pending generation or pre-push
  DELIVERED: "delivered",               // in Outlook drafts folder
  USER_DELETED: "user_deleted",         // replaces draft_deleted boolean (Decision 1b)
  EDITED_IN_OUTLOOK: "edited_in_outlook",   // reserved: iteration 2+
  SENT: "sent",                         // reserved: iteration 2+
  STALE: "stale",                       // reserved: iteration 2+
});

// emails.deferred_reason — why a row landed in 'skipped' instead of 'done'.
const DeferredReason = Object.freeze({
  ONBOARDING: "onboarding",
  USER_DISMISSED: "user_dismissed",
});

// ============================================================
// Legacy-to-new vocabulary mappings.
// ============================================================

const EMAIL_LEGACY_TO_NEW = Object.freeze({
  [LegacyEmailStatus.UNPROCESSED]: Status.PENDING,
  [LegacyEmailStatus.PROCESSING]: Status.ACTIVE,
  [LegacyEmailStatus.PROCESSED]: Status.DONE,
  [LegacyEmailStatus.COMPLETED]: Status.DONE,
  [LegacyEmailStatus.ERROR]: Status.FAILED,
  [LegacyEmailStatus.ONBOARDING]: Status.SKIPPED,
  [LegacyEmailStatus.DISMISSED]: Status.SKIPPED,
});

const DRAFT_LEGACY_TO_NEW = Object.freeze({
  [LegacyDraftStatus.PENDING]: Status.PENDING,
  [LegacyDraftStatus.WRITTEN]: Status.DONE,
  [LegacyDraftStatus.DELETED]: Status.SKIPPED,
});

const PIPELINE_RUN_LEGACY_TO_NEW = Object.freeze({
  [LegacyPipelineRunStatus.RUNNING]: Status.ACTIVE,
  [LegacyPipelineRunStatus.COMPLETED]: Status.DONE,
  [LegacyPipelineRunStatus.FAILED]: Status.FAILED,
  [LegacyPipelineRunStatus.PARTIAL_FAILURE]: Status.DONE,
});

// ============================================================
// Dual-read helpers.
// Return arrays of accepted values during Phase 2, so filter queries
// can match both legacy and new vocabulary without branching on phase.
// ============================================================

function emailPendingValues() {
  return [LegacyEmailStatus.UNPROCESSED, Status.PENDING];
}

function emailActiveValues() {
  return [LegacyEmailStatus.PROCESSING, Status.ACTIVE];
}

function emailDoneValues() {
  return [
    LegacyEmailStatus.PROCESSED,
    LegacyEmailStatus.COMPLETED,
    Status.DONE,
  ];
}

function draftPendingValues() {
  return [LegacyDraftStatus.PENDING, Status.PENDING];
}

function draftDeliveredValues() {
  return [LegacyDraftStatus.WRITTEN, Status.DONE];
}

function pipelineRunActiveValues() {
  return [LegacyPipelineRunStatus.RUNNING, Status.ACTIVE];
}

// Emails a user has resolved from the inbox view — legacy completed/dismissed
// plus their new-vocabulary equivalents (done / skipped-with-user_dismissed).
// Over-matches skipped-for-onboarding emails by one status value; callers that
// need to distinguish must check deferred_reason.
function emailResolvedValues() {
  return [
    LegacyEmailStatus.COMPLETED,
    LegacyEmailStatus.DISMISSED,
    Status.DONE,
    Status.SKIPPED,
  ];
}

// Values used to exclude tombstoned drafts from inbox joins.
// Legacy 'deleted' and its new-vocabulary equivalent 'skipped'.
function draftExcludedValues() {
  return [LegacyDraftStatus.DELETED, Status.SKIPPED];
}
