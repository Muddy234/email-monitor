-- Rollback for migration 041_unify_status_vocabulary.sql
--
-- This file lives OUTSIDE supabase/migrations/ so the Supabase CLI does
-- NOT auto-apply it. Run manually only if Phase 3 needs reverting:
--
--   supabase db execute --file supabase/rollbacks/041_unify_status_vocabulary_rollback.sql
--
-- Trigger conditions: unexpected worker errors, UI failures, or data
-- corruption observed in the first hour after 041 deploy.
--
-- Notes / fidelity caveats:
--   1. Forward migration collapsed worker 'processed' and dashboard
--      'completed' into 'done'. This rollback restores ALL such rows to
--      'processed' (the safer choice — both legacy values are treated
--      as terminal-success by readers). If stricter fidelity is needed,
--      restore from the pre-migration DB snapshot instead.
--   2. Forward migration also wiped failure_count to 0. There is no way
--      to reconstruct the previous values from inside the DB; the
--      pre-migration snapshot is the only path back.
--   3. RPC bodies were NOT changed in 041 (kept dual-read from 040), so
--      no RPC restore is needed here.

begin;

set local statement_timeout = '10min';
set local lock_timeout = '30s';

-- Disable the bump_state_entered_at triggers for the duration of the
-- bulk reverse-UPDATEs, mirroring the forward migration.
alter table public.emails disable trigger emails_bump_state;
alter table public.drafts disable trigger drafts_bump_state;
alter table public.pipeline_runs disable trigger pipeline_runs_bump_state;
alter table public.profiles disable trigger profiles_bump_onboarding_state;

-- ============================================================
-- EMAILS — restore legacy vocabulary
-- ============================================================
update public.emails set status = 'onboarding'
  where status = 'skipped' and deferred_reason = 'onboarding';
update public.emails set status = 'dismissed'
  where status = 'skipped' and deferred_reason = 'user_dismissed';
update public.emails set status = 'unprocessed' where status = 'pending';
update public.emails set status = 'processing' where status = 'active';
-- See note (1): collapses to 'processed' by choice.
update public.emails set status = 'processed' where status = 'done';
update public.emails set status = 'error' where status = 'failed';

-- Clear deferred_reason rows we just consumed (legacy schema had no
-- semantic meaning for them outside the skipped status).
update public.emails set deferred_reason = null
  where status in ('onboarding', 'dismissed');

-- ============================================================
-- DRAFTS — restore legacy vocabulary
-- ============================================================
update public.drafts set status = 'written'
  where status = 'done' and delivery_state = 'delivered';
update public.drafts
  set status = 'deleted',
      draft_deleted = true
  where status = 'skipped' and delivery_state = 'user_deleted';
-- 'pending' status name is unchanged.

-- ============================================================
-- PIPELINE_RUNS — restore legacy vocabulary
-- ============================================================
update public.pipeline_runs set status = 'running' where status = 'active';
update public.pipeline_runs set status = 'partial_failure'
  where status = 'done' and has_partial_failures;
update public.pipeline_runs set status = 'completed' where status = 'done';
-- 'failed' unchanged.

-- ============================================================
-- PROFILES — no forward changes to revert (iteration 1 deferred)
-- ============================================================

-- ============================================================
-- Restore legacy column DEFAULTs.
-- ============================================================
alter table public.emails        alter column status set default 'unprocessed';
alter table public.pipeline_runs alter column status set default 'running';
-- public.drafts.status default was 'pending' both before and after
-- the forward migration — no change needed.

-- ============================================================
-- Re-enable triggers.
-- ============================================================
alter table public.emails enable trigger emails_bump_state;
alter table public.drafts enable trigger drafts_bump_state;
alter table public.pipeline_runs enable trigger pipeline_runs_bump_state;
alter table public.profiles enable trigger profiles_bump_onboarding_state;

commit;
