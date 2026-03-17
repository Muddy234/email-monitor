-- NOTE: Shares 007 prefix with 007_response_events_features.sql. Both are idempotent.
-- Add display_name to profiles for draft sign-offs.
-- Captured during signup; used by the worker when generating drafts.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS display_name text DEFAULT '';
