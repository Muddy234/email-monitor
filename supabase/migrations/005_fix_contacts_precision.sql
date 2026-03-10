-- Fix contacts table: precision issues and missing columns.
--
-- Precision fixes:
--   emails_per_month was integer, truncating decimals (e.g. 1.8 → 1).
--   response_rate was numeric(5,2), truncating to 2 decimals when
--   extraction produces 4 (e.g. 0.5625 → 0.56).
--
-- New columns:
--   total_received — actual email count used for Bayesian smoothing
--   reply_rate_30d / reply_rate_90d — time-windowed response rates
--   smoothed_rate — Bayesian-smoothed response rate from onboarding
--   median_response_time_hours — complement to avg
--   forward_rate — fraction of emails that are forwards
--   typical_subjects — top recurring subject lines

alter table public.contacts
    alter column emails_per_month type numeric(8,1) using emails_per_month::numeric(8,1),
    alter column response_rate type numeric(5,4) using response_rate::numeric(5,4);

alter table public.contacts
    add column if not exists total_received integer default 0,
    add column if not exists reply_rate_30d numeric(5,4),
    add column if not exists reply_rate_90d numeric(5,4),
    add column if not exists smoothed_rate numeric(5,4),
    add column if not exists median_response_time_hours numeric(8,2),
    add column if not exists forward_rate numeric(5,4),
    add column if not exists typical_subjects text[] default '{}';
