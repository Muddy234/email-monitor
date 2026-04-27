-- Phase 2 of state management refactor — RPC dual-read.
--
-- Makes read-side RPCs accept both legacy and new status vocabularies.
-- Writers still emit legacy values; the writer flip happens in Phase 4.
--
-- Both RPCs below are replaced verbatim (preserving existing bodies) with
-- only the status filter clauses widened to include new-vocabulary values.
-- CREATE OR REPLACE replaces the entire function body, so the full body
-- must be present even for small changes.

-- ============================================================
-- claim_unprocessed_emails — atomic claim-and-process.
-- Original (001_initial_schema.sql): status = 'unprocessed'.
-- Phase 2 widening:                  status in ('unprocessed', 'pending').
-- Writer still sets status = 'processing' (unchanged until Phase 4).
-- ============================================================
create or replace function public.claim_unprocessed_emails(
    p_user_id uuid,
    p_limit integer default 10
)
returns setof public.emails as $$
begin
    return query
    update public.emails
    set status = 'processing'
    where id in (
        select id from public.emails
        where status in ('unprocessed', 'pending')  -- DUAL-READ (Phase 2)
          and user_id = p_user_id
        order by received_time asc
        limit p_limit
        for update skip locked
    )
    returning *;
end;
$$ language plpgsql security definer;

-- ============================================================
-- find_stale_drafts — extension calls this to reap drafts whose
-- conversation has been replied to elsewhere.
-- Original (020_draft_cleanup.sql): status IN ('written', 'pending').
-- Phase 2 widening:                 status IN ('written', 'pending', 'done').
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
    and d.status in ('written', 'pending', 'done')  -- DUAL-READ (Phase 2)
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
