-- Phase 3 of state management refactor — unify status vocabulary.
--
-- Rewrites every existing emails / drafts / pipeline_runs row to the
-- unified vocabulary (pending / active / done / failed / skipped) and
-- flips column defaults so new inserts land in the new vocabulary.
--
-- Writers remain on legacy vocabulary until Phase 4 ships. To avoid a
-- silent-stuck-row window during the hybrid period, the dual-read RPCs
-- introduced in migration 040 are NOT narrowed here. They will be
-- narrowed alongside the writer flip / CHECK-constraint addition in
-- Phase 5 (migration 043).
--
-- Iteration 1 scope: drafts-only reaper. profiles.onboarding_status is
-- DEFERRED until iteration 2; the corresponding remap block is omitted.
--
-- ⚠ This migration disables the bump_state_entered_at triggers. Without
-- this, every UPDATE here would overwrite state_entered_at (set by Phase
-- 1 backfill) with now(), poisoning the diagnostic substrate.

begin;

-- Fail fast if the migration cannot make progress.
set local statement_timeout = '10min';
set local lock_timeout = '30s';

-- ============================================================
-- Disable state-bump triggers for the duration of the bulk UPDATEs.
-- Re-enabled before COMMIT.
-- ============================================================
alter table public.emails disable trigger emails_bump_state;
alter table public.drafts disable trigger drafts_bump_state;
alter table public.pipeline_runs disable trigger pipeline_runs_bump_state;
alter table public.profiles disable trigger profiles_bump_onboarding_state;

-- ============================================================
-- Wipe legacy failure_count values. The trigger did not maintain
-- these before this migration, so the columns are unreliable.
-- Reset to 0 so the trigger is the sole writer going forward.
-- ============================================================
update public.emails set failure_count = 0;
update public.drafts set failure_count = 0;
update public.pipeline_runs set failure_count = 0;
update public.profiles set onboarding_failure_count = 0
  where onboarding_failure_count is not null;

-- ============================================================
-- EMAILS
-- Legacy values observed in production:
--   unprocessed, processing, processed, onboarding, error,
--   completed, dismissed
-- ============================================================

-- Reset any stragglers still in 'processing' (the worker should have
-- been stopped before this migration; the pre-flight reset handles
-- most, this catches the rest).
update public.emails set status = 'unprocessed'
  where status = 'processing';

-- Preserve 'onboarding' deferral reason
update public.emails
  set deferred_reason = 'onboarding',
      status = 'skipped'
  where status = 'onboarding';

-- Preserve dashboard 'dismissed' user action
update public.emails
  set deferred_reason = 'user_dismissed',
      status = 'skipped'
  where status = 'dismissed';

-- Standard remap
update public.emails set status = 'pending' where status = 'unprocessed';
update public.emails set status = 'done'
  where status in ('processed', 'completed');
update public.emails set status = 'failed' where status = 'error';

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.emails
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % emails with invalid status after migration', bad_count;
  end if;
end $$;

-- ============================================================
-- DRAFTS
-- Legacy values observed in production: pending, written, deleted.
-- Forward migration also subsumes the draft_deleted boolean into
-- delivery_state.
-- ============================================================

-- 'written' → done + delivered
update public.drafts
  set delivery_state = 'delivered',
      status = 'done'
  where status = 'written';

-- 'deleted' → skipped + user_deleted (replaces the draft_deleted boolean)
update public.drafts
  set delivery_state = 'user_deleted',
      status = 'skipped'
  where status = 'deleted';

-- 'pending' rows: status name is unchanged, but ensure delivery_state
-- has a sensible default.
update public.drafts
  set delivery_state = coalesce(delivery_state, 'not_delivered');

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.drafts
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % drafts with invalid status after migration', bad_count;
  end if;
end $$;

-- ============================================================
-- PIPELINE_RUNS
-- Legacy values observed in production:
--   running, completed, failed, partial_failure
-- ============================================================

update public.pipeline_runs set status = 'active' where status = 'running';
update public.pipeline_runs
  set has_partial_failures = true,
      status = 'done'
  where status = 'partial_failure';
update public.pipeline_runs set status = 'done' where status = 'completed';
-- 'failed' unchanged

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.pipeline_runs
  where status not in ('pending', 'active', 'done', 'failed');
  if bad_count > 0 then
    raise exception 'Found % pipeline_runs with invalid status after migration', bad_count;
  end if;
end $$;

-- ============================================================
-- PROFILES (onboarding_status)   [DEFERRED — iteration 2+]
-- The drafts-only iteration leaves profiles.onboarding_status on the
-- 12-state legacy vocabulary. _recover_stuck_onboarding continues to
-- own onboarding stuck-detection until iteration 2 ships scope C.
-- ============================================================
-- (intentionally no UPDATE here)

-- ============================================================
-- Flip column DEFAULTs so new inserts land in the unified vocabulary.
-- Writers that omit `status` on INSERT (extension/background.js
-- relies on this for newly ingested emails) need the default to be
-- new-vocab post-migration.
-- ============================================================
alter table public.emails        alter column status set default 'pending';
alter table public.pipeline_runs alter column status set default 'active';
-- public.drafts.status default is already 'pending' — no change needed.

-- ============================================================
-- Re-enable triggers. From this point on, state_entered_at and
-- failure_count are maintained by the trigger on every UPDATE.
-- ============================================================
alter table public.emails enable trigger emails_bump_state;
alter table public.drafts enable trigger drafts_bump_state;
alter table public.pipeline_runs enable trigger pipeline_runs_bump_state;
alter table public.profiles enable trigger profiles_bump_onboarding_state;

commit;

-- ============================================================
-- After this migration commits, re-add tables to the realtime
-- publication if they were dropped during the pre-flight (run
-- outside this transaction):
--
--   alter publication supabase_realtime add table public.emails;
--   alter publication supabase_realtime add table public.drafts;
--   alter publication supabase_realtime add table public.pipeline_runs;
--   alter publication supabase_realtime add table public.profiles;
-- ============================================================
