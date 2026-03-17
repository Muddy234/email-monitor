-- NOTE: Shares 003 prefix with 003_onboarding_schema.sql. Both are idempotent (IF NOT EXISTS). Safe to run in any order.
-- Add heartbeat and timezone columns for activity-based worker gating.
-- The extension writes last_heartbeat_at on every sync cycle (~5 min).
-- The worker checks staleness + business hours before processing a user.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz,
  ADD COLUMN IF NOT EXISTS timezone text DEFAULT 'America/Chicago';
