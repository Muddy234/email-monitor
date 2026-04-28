-- Reaper kill switch substrate.
--
-- Provides a small key/value table the worker reads to decide whether to
-- run the reaper on a given tick. RLS is enabled with no policies, so only
-- the service_role (which bypasses RLS) can read or write. Operators flip
-- the kill switch via direct SQL.
--
-- Two seeded keys:
--   reaper_enabled        boolean  — master on/off
--   reaper_paused_until   timestamptz-as-jsonb-string OR null — temporary pause
--
-- Worker side: worker/config.py reads with a 60s TTL cache.

begin;

set local statement_timeout = '1min';
set local lock_timeout = '30s';

create table if not exists public.system_config (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

alter table public.system_config enable row level security;

-- No policies created. RLS enabled with no policies = service-role only.

insert into public.system_config (key, value) values
    ('reaper_enabled',       'true'::jsonb),
    ('reaper_paused_until',  'null'::jsonb)
on conflict (key) do nothing;

commit;
