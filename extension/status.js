/**
 * Status vocabulary constants for the Email_Monitor Chrome extension.
 *
 * Mirrors worker/status.py. Loaded as a classic (non-module) global script
 * so it works in both the MV3 service worker (via importScripts) and the
 * popup (via <script> tag).
 *
 * Phase 5 stripped the legacy classes, mapping tables, and dual-read helpers
 * that bridged the migration from the pre-unified vocabularies. Migration
 * 045 added CHECK constraints so the DB now rejects non-unified values.
 *
 * LegacyOnboardingStatus is preserved — onboarding uses its own multi-stage
 * FSM that is intentionally separate from email/draft/pipeline_run status.
 */

// Unified lifecycle vocabulary. Target state for every status column.
const Status = Object.freeze({
  PENDING: "pending",
  ACTIVE: "active",
  DONE: "done",
  FAILED: "failed",
  SKIPPED: "skipped",
});

// Values written to profiles.onboarding_status.
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
