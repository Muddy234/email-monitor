-- Hide raw `last_error` text from anon and authenticated readers.
--
-- last_error contains diagnostic strings written by the worker. Even after
-- sanitize_error() strips paths and secret-like patterns, the raw message
-- can still leak internal details unsuited for the dashboard. Revoke
-- column-level SELECT and expose a sanitized signal (e.g. error_category)
-- separately if a user-facing summary is ever needed.
--
-- Service-role bypasses RLS and column grants, so the worker continues to
-- read/write last_error normally.

begin;

set local statement_timeout = '1min';
set local lock_timeout = '30s';

revoke select (last_error) on public.emails        from anon, authenticated;
revoke select (last_error) on public.drafts        from anon, authenticated;
revoke select (last_error) on public.pipeline_runs from anon, authenticated;
revoke select (onboarding_last_error) on public.profiles from anon, authenticated;

commit;
