-- Rollback for migration 043_phase4_cleanup.sql
--
-- This file lives OUTSIDE supabase/migrations/ so the Supabase CLI does
-- NOT auto-apply it. Run manually only if Phase 4.5 needs reverting:
--
--   supabase db execute --file supabase/rollbacks/043_phase4_cleanup_rollback.sql
--
-- The row-level data backfills are NOT reversed — once a stale legacy row
-- has been remapped to unified vocabulary, restoring it would require
-- knowing each row's pre-cleanup status, which is not preserved. The only
-- thing this rollback restores is the claim_unprocessed_emails RPC body
-- to its 040_rpc_dual_read.sql form (writes legacy 'processing').

begin;

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

commit;
