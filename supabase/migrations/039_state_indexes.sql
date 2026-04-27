-- Phase 1 of state management refactor (part 3).
-- Partial indexes on (status, state_entered_at) for every status-bearing
-- table. These support the reaper's "how long has this row been in this
-- state?" query and keep Phase 2 dual-read queries fast.
--
-- Predicates intentionally include both legacy AND new vocabulary values
-- so the same index survives the Phase 3 cutover without a window of
-- unindexed query plans. The Phase 5 migration (043a/043b) tightens
-- predicates to new-only once legacy values are gone.

begin;

set local statement_timeout = '10min';
set local lock_timeout = '30s';

-- emails: current writers produce 'unprocessed'/'processing', future
-- vocabulary is 'pending'/'active'. Index covers both.
create index if not exists idx_emails_state_entered
    on public.emails (status, state_entered_at)
    where status in ('pending', 'active', 'unprocessed', 'processing');

-- drafts: 'pending' is stable across legacy and new. 'active' is reserved
-- for future (currently unused on drafts). Include both for forward
-- compatibility.
create index if not exists idx_drafts_state_entered
    on public.drafts (status, state_entered_at)
    where status in ('pending', 'active');

-- pipeline_runs: current writers produce 'running', future is 'active'.
create index if not exists idx_pipeline_runs_state_entered
    on public.pipeline_runs (status, state_entered_at)
    where status in ('pending', 'active', 'running');

-- profiles: onboarding_status uses a 12-state vocabulary today and stays
-- legacy in iteration 1. Index everything NOT in a terminal state so the
-- reaper can find stuck onboarding rows regardless of which intermediate
-- value they land on.
create index if not exists idx_profiles_onboarding_state_entered
    on public.profiles (onboarding_status, onboarding_state_entered_at)
    where onboarding_status is not null
      and onboarding_status not in ('complete', 'complete_partial', 'failed', 'done', 'skipped');

commit;
