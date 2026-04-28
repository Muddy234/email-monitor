-- Phase 5 lockdown — pin the unified status vocabulary in the database.
--
-- Migration 043 drained every residual legacy value, and Phase 4/4.5 flipped
-- every writer (worker, extension, web, RPCs) to the unified vocabulary.
-- This migration:
--   1. Adds CHECK constraints so the DB rejects any legacy value going
--      forward (the application code can no longer write them, and the
--      Legacy* classes have been deleted from worker/extension/web).
--   2. Narrows the dual-read RPCs (claim_unprocessed_emails, find_stale_drafts)
--      so they only accept unified vocabulary on the read side. The write
--      side was already unified by 043.
--
-- Vocabulary:
--   emails.status, drafts.status: pending | active | done | failed | skipped
--   pipeline_runs.status:         pending | active | done | failed
--                                 (skipped is N/A — runs always finish)

begin;

set local statement_timeout = '5min';
set local lock_timeout = '30s';

-- ============================================================
-- Sanity check — fail fast if any legacy values slipped past 043.
-- ============================================================
do $$
declare
  bad_emails        integer;
  bad_drafts        integer;
  bad_pipeline_runs integer;
begin
  select count(*) into bad_emails
  from public.emails
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');

  select count(*) into bad_drafts
  from public.drafts
  where status not in ('pending', 'active', 'done', 'failed', 'skipped');

  select count(*) into bad_pipeline_runs
  from public.pipeline_runs
  where status not in ('pending', 'active', 'done', 'failed');

  if bad_emails > 0 then
    raise exception 'Cannot lock vocabulary: % emails have non-unified status', bad_emails;
  end if;
  if bad_drafts > 0 then
    raise exception 'Cannot lock vocabulary: % drafts have non-unified status', bad_drafts;
  end if;
  if bad_pipeline_runs > 0 then
    raise exception 'Cannot lock vocabulary: % pipeline_runs have non-unified status', bad_pipeline_runs;
  end if;
end $$;

-- ============================================================
-- CHECK constraints — lock the vocabulary.
-- Named so they can be dropped individually if rollback is needed.
-- ============================================================
alter table public.emails
  add constraint emails_status_unified_check
  check (status in ('pending', 'active', 'done', 'failed', 'skipped'));

alter table public.drafts
  add constraint drafts_status_unified_check
  check (status in ('pending', 'active', 'done', 'failed', 'skipped'));

alter table public.pipeline_runs
  add constraint pipeline_runs_status_unified_check
  check (status in ('pending', 'active', 'done', 'failed'));

-- ============================================================
-- claim_unprocessed_emails — drop dual-read; pending-only.
-- Original: 001_initial_schema.sql ('unprocessed').
-- Phase 2:  040_rpc_dual_read.sql ('unprocessed', 'pending').
-- Phase 4.5:043_phase4_cleanup.sql (writes 'active'; still dual-reads).
-- Phase 5:  this migration — read 'pending' only.
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
        where status = 'pending'
          and user_id = p_user_id
        order by received_time asc
        limit p_limit
        for update skip locked
    )
    returning *;
end;
$$ language plpgsql security definer;

-- ============================================================
-- find_stale_drafts — drop dual-read; unified-only.
-- Original: 020_draft_cleanup.sql ('written', 'pending').
-- Phase 2:  040_rpc_dual_read.sql ('written', 'pending', 'done').
-- Phase 5:  this migration — read ('pending', 'done') only.
-- ============================================================
create or replace function public.find_stale_drafts()
returns table (
  draft_id uuid,
  outlook_draft_id text,
  status text
)
language plpgsql
security invoker
as $$
begin
  return query
  select d.id        as draft_id,
         d.outlook_draft_id,
         d.status
  from public.drafts d
  join public.emails parent on parent.id = d.email_id
  where d.user_id = auth.uid()
    and d.status in ('pending', 'done')
    and d.draft_deleted = false
    and exists (
      select 1
      from public.emails sent
      where sent.user_id  = auth.uid()
        and sent.conversation_id = parent.conversation_id
        and sent.folder    = 'Sent Items'
        and sent.received_time > d.created_at
    )
  limit 10;
end;
$$;

commit;
