-- Rollback for migration 045_lock_unified_vocabulary.sql
--
-- Lives OUTSIDE supabase/migrations/ so the Supabase CLI does NOT auto-apply
-- it. Run manually only if Phase 5 needs reverting:
--
--   supabase db execute --file supabase/rollbacks/045_lock_unified_vocabulary_rollback.sql
--
-- Drops the CHECK constraints and restores the dual-read RPC bodies to their
-- 040_rpc_dual_read.sql / 043_phase4_cleanup.sql forms.

begin;

-- ============================================================
-- Drop CHECK constraints.
-- ============================================================
alter table public.emails
  drop constraint if exists emails_status_unified_check;
alter table public.drafts
  drop constraint if exists drafts_status_unified_check;
alter table public.pipeline_runs
  drop constraint if exists pipeline_runs_status_unified_check;

-- ============================================================
-- Restore claim_unprocessed_emails to 043_phase4_cleanup.sql form
-- (writes unified 'active', dual-reads 'unprocessed'/'pending').
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
        where status in ('unprocessed', 'pending')
          and user_id = p_user_id
        order by received_time asc
        limit p_limit
        for update skip locked
    )
    returning *;
end;
$$ language plpgsql security definer;

-- ============================================================
-- Restore find_stale_drafts to 040_rpc_dual_read.sql form
-- (dual-reads 'written'/'pending'/'done').
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
    and d.status in ('written', 'pending', 'done')
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
