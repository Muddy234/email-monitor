-- Phase 4 of state management refactor — extension version telemetry.
--
-- Adds a column the extension stamps on every draft write so Phase 5 can
-- gate legacy-vocabulary removal on a measurable rollout signal:
--
--   "≥99% of drafts.updated_at writes in the last 7 days carry a
--    written_by_extension_version >= the first Phase-4-shipping build."
--
-- Without this column, Phase 5 would have to guess when the last legacy
-- writer drained from the extension fleet. The column is intentionally
-- NULLable — pre-Phase-4 builds will never write it, and that NULL is
-- itself the signal that a legacy writer is still in circulation.

begin;

alter table public.drafts
  add column if not exists written_by_extension_version text;

comment on column public.drafts.written_by_extension_version is
  'manifest.json version of the extension that last wrote this row. '
  'NULL for pre-Phase-4 writes. Phase 5 cutover gates on this column '
  'showing ≥99% non-NULL recent writes for 7+ days.';

commit;
