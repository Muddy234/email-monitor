-- Phase 4.5 cleanup — drain residual legacy values + flip the last SQL writer.
--
-- Migration 041 (Phase 3) backfilled every existing row to the unified
-- vocabulary, but writers stayed on legacy values until Phase 4 shipped.
-- Any rows written between 041 and the Phase 4 worker deploy still carry
-- legacy values; this migration drains them so Phase 5 can add the CHECK
-- constraint without manual cleanup.
--
-- Also flips the one SQL-layer writer Phase 4 missed:
-- claim_unprocessed_emails RPC still hard-codes `set status = 'processing'`.
--
-- Bulk UPDATEs disable the bump_state_entered_at triggers (same pattern as
-- migration 041) so this cleanup does not poison state_entered_at.

begin;

set local statement_timeout = '5min';
set local lock_timeout = '30s';

-- ============================================================
-- Disable state-bump triggers for the duration of the bulk UPDATEs.
-- ============================================================
alter table public.emails disable trigger emails_bump_state;
alter table public.drafts disable trigger drafts_bump_state;
alter table public.pipeline_runs disable trigger pipeline_runs_bump_state;

-- ============================================================
-- EMAILS — drain residual legacy values.
-- Same remap as migration 041; idempotent if no legacy rows remain.
-- ============================================================
update public.emails
  set deferred_reason = 'onboarding',
      status = 'skipped'
  where status = 'onboarding';

update public.emails
  set deferred_reason = 'user_dismissed',
      status = 'skipped'
  where status = 'dismissed';

update public.emails set status = 'pending' where status = 'unprocessed';
update public.emails set status = 'active'  where status = 'processing';
update public.emails set status = 'done'    where status in ('processed', 'completed');
update public.emails set status = 'failed'  where status = 'error';

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.emails
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % emails with invalid status after cleanup', bad_count;
  end if;
end $$;

-- ============================================================
-- DRAFTS — drain residual legacy values.
-- ============================================================
update public.drafts
  set delivery_state = 'delivered',
      status = 'done'
  where status = 'written';

update public.drafts
  set delivery_state = 'user_deleted',
      status = 'skipped'
  where status = 'deleted';

-- Backfill delivery_state for any pending row missing it.
update public.drafts
  set delivery_state = 'not_delivered'
  where delivery_state is null
    and status = 'pending';

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.drafts
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');
  if bad_count > 0 then
    raise exception 'Found % drafts with invalid status after cleanup', bad_count;
  end if;
end $$;

-- ============================================================
-- PIPELINE_RUNS — drain residual legacy values.
-- ============================================================
update public.pipeline_runs set status = 'active' where status = 'running';
update public.pipeline_runs
  set has_partial_failures = true,
      status = 'done'
  where status = 'partial_failure';
update public.pipeline_runs set status = 'done' where status = 'completed';
-- 'failed' unchanged (legacy and unified are the same string).

do $$
declare
  bad_count integer;
begin
  select count(*) into bad_count
  from public.pipeline_runs
  where status not in ('pending', 'active', 'done', 'failed');
  if bad_count > 0 then
    raise exception 'Found % pipeline_runs with invalid status after cleanup', bad_count;
  end if;
end $$;

-- ============================================================
-- Re-enable triggers.
-- ============================================================
alter table public.emails enable trigger emails_bump_state;
alter table public.drafts enable trigger drafts_bump_state;
alter table public.pipeline_runs enable trigger pipeline_runs_bump_state;

-- ============================================================
-- claim_unprocessed_emails — flip the last SQL writer to unified vocab.
--
-- Phase 4 flipped every application-layer writer but missed this RPC body.
-- Read filter widens to include 'active' so the function still claims rows
-- the worker may have left mid-flight under either vocabulary; the write
-- now emits 'active' instead of legacy 'processing'.
--
-- Original (001_initial_schema.sql) → 040_rpc_dual_read.sql → here.
-- ============================================================
create or replace function public.claim_unprocessed_emails(
    p_user_id uuid,
    p_limit integer default 10
)
returns setof public.emails as $$
begin
    return query
    update public.emails
    set status = 'active'
    where id in (
        select id from public.emails
        where status in ('unprocessed', 'pending')  -- DUAL-READ during hybrid window
          and user_id = p_user_id
        order by received_time asc
        limit p_limit
        for update skip locked
    )
    returning *;
end;
$$ language plpgsql security definer;

commit;
