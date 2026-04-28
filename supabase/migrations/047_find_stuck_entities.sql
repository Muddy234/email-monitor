-- find_stuck_entities — single RPC the reaper calls every cycle.
--
-- Returns rows from emails / drafts / pipeline_runs that have been sitting
-- in `status='active'` longer than their per-entity timeout.
--
-- Timeouts (locked, iteration 1):
--   emails         10 min
--   drafts         10 min
--   pipeline_runs  60 min  (accommodates Anthropic Batches API runtime)
--
-- Reads `state_entered_at` (the lifecycle clock from migration 037).
-- Rows with state_entered_at IS NULL are skipped — by NOT NULL constraint
-- they shouldn't exist, but the filter is defensive.
--
-- failure_count is included so the reaper can decide retry vs. fail.
-- COALESCE wraps it defensively; the column is NOT NULL default 0.
--
-- Service-role only (no RLS exposure).

begin;

set local statement_timeout = '1min';
set local lock_timeout = '30s';

create or replace function public.find_stuck_entities()
returns table (
    entity         text,
    id             uuid,
    user_id        uuid,
    failure_count  integer,
    state_entered_at timestamptz
)
language sql
security definer
set search_path = public
as $$
    select
        'emails'::text          as entity,
        e.id                    as id,
        e.user_id               as user_id,
        coalesce(e.failure_count, 0) as failure_count,
        e.state_entered_at      as state_entered_at
    from public.emails e
    where e.status = 'active'
      and e.state_entered_at is not null
      and e.state_entered_at < now() - interval '10 minutes'

    union all

    select
        'drafts'::text          as entity,
        d.id                    as id,
        d.user_id               as user_id,
        coalesce(d.failure_count, 0) as failure_count,
        d.state_entered_at      as state_entered_at
    from public.drafts d
    where d.status = 'active'
      and d.state_entered_at is not null
      and d.state_entered_at < now() - interval '10 minutes'

    union all

    select
        'pipeline_runs'::text   as entity,
        p.id                    as id,
        p.user_id               as user_id,
        coalesce(p.failure_count, 0) as failure_count,
        p.state_entered_at      as state_entered_at
    from public.pipeline_runs p
    where p.status = 'active'
      and p.state_entered_at is not null
      and p.state_entered_at < now() - interval '60 minutes'
$$;

revoke all on function public.find_stuck_entities() from public, anon, authenticated;
grant execute on function public.find_stuck_entities() to service_role;

commit;
