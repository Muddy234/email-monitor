-- Rollback for migration 042_extension_version_telemetry.sql
--
-- This file lives OUTSIDE supabase/migrations/ so the Supabase CLI does
-- NOT auto-apply it. Run manually only if Phase 4 needs reverting:
--
--   supabase db execute --file supabase/rollbacks/042_extension_version_telemetry_rollback.sql
--
-- Drops the column unconditionally. No data needs to be preserved — the
-- column is pure write-side telemetry; nothing reads it except Phase 5
-- gating queries which run ad-hoc.

begin;

alter table public.drafts
  drop column if exists written_by_extension_version;

commit;
