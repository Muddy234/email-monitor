-- Migration 009: Add missing contacts columns required by upsert_contacts().
--
-- These columns are populated during onboarding synthesis and used by the
-- scorer for per-sender response modeling. Without them, upsert_contacts()
-- fails because Postgres rejects writes to non-existent columns.
--
-- Note: DDL in Postgres is auto-transactional — if any ALTER fails, the
-- entire statement rolls back. The explicit BEGIN/COMMIT is belt-and-suspenders
-- and doesn't change behavior here, but documents intent.

BEGIN;

ALTER TABLE public.contacts
  ADD COLUMN IF NOT EXISTS reply_rate_30d     double precision,
  ADD COLUMN IF NOT EXISTS reply_rate_90d     double precision,
  ADD COLUMN IF NOT EXISTS smoothed_rate      double precision,
  ADD COLUMN IF NOT EXISTS median_response_time_hours double precision,
  ADD COLUMN IF NOT EXISTS forward_rate       double precision,
  ADD COLUMN IF NOT EXISTS typical_subjects   text[] DEFAULT '{}';

COMMENT ON COLUMN public.contacts.reply_rate_30d IS
  'Fraction of emails from this sender the user replied to in the last 30 days';
COMMENT ON COLUMN public.contacts.reply_rate_90d IS
  'Fraction of emails from this sender the user replied to in the last 90 days';
COMMENT ON COLUMN public.contacts.smoothed_rate IS
  'Bayesian-smoothed response rate: (raw_rate * n + prior_weight * global_rate) / (n + prior_weight)';
COMMENT ON COLUMN public.contacts.median_response_time_hours IS
  'Median hours between receiving an email from this sender and the user replying';
COMMENT ON COLUMN public.contacts.forward_rate IS
  'Fraction of emails from this sender the user forwarded rather than replied to';
COMMENT ON COLUMN public.contacts.typical_subjects IS
  'Array of representative subject lines from this sender, used for topic matching';

COMMIT;
