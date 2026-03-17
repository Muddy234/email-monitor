-- NOTE: Shares 006 prefix with 006_reset_stuck_emails.sql. Both are idempotent.
-- Add scoring output columns to response_events for pipeline trace.
-- Also add delivered_at to drafts for delivery tracking.

ALTER TABLE public.response_events
  ADD COLUMN IF NOT EXISTS raw_score numeric(10,6),
  ADD COLUMN IF NOT EXISTS calibrated_prob numeric(10,6),
  ADD COLUMN IF NOT EXISTS confidence_tier text,
  ADD COLUMN IF NOT EXISTS gate_reason text;

ALTER TABLE public.drafts
  ADD COLUMN IF NOT EXISTS delivered_at timestamptz;
