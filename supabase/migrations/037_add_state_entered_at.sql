-- Phase 1 of state management refactor.
-- Additive only: adds state_entered_at, failure_count, last_error, and
-- auxiliary columns (deferred_reason, delivery_state, has_partial_failures,
-- onboarding substate columns) to every status-bearing table. Backfills
-- state_entered_at from the best available timestamp per table, then makes
-- the column NOT NULL with default now().
--
-- Safe to apply with the worker running: no existing columns are touched,
-- no status values are rewritten. Triggers that maintain these columns
-- are added in migration 038.
--
-- Per-table timestamp availability (v4 reconciliation):
--   emails:         created_at only
--   drafts:         created_at + updated_at
--   pipeline_runs:  started_at + finished_at
--   profiles:       onboarding_started_at + created_at

begin;

set local statement_timeout = '10min';
set local lock_timeout = '30s';

-- ============================================================
-- emails
-- ============================================================
alter table public.emails
    add column if not exists state_entered_at timestamptz,
    add column if not exists failure_count integer default 0 not null,
    add column if not exists last_error text,
    add column if not exists deferred_reason text;

update public.emails
    set state_entered_at = coalesce(created_at, now())
    where state_entered_at is null;

alter table public.emails
    alter column state_entered_at set not null,
    alter column state_entered_at set default now();

-- ============================================================
-- drafts
-- ============================================================
alter table public.drafts
    add column if not exists state_entered_at timestamptz,
    add column if not exists failure_count integer default 0 not null,
    add column if not exists last_error text,
    add column if not exists delivery_state text;

update public.drafts
    set state_entered_at = coalesce(updated_at, created_at, now())
    where state_entered_at is null;

alter table public.drafts
    alter column state_entered_at set not null,
    alter column state_entered_at set default now();

-- ============================================================
-- pipeline_runs
-- ============================================================
alter table public.pipeline_runs
    add column if not exists state_entered_at timestamptz,
    add column if not exists failure_count integer default 0 not null,
    add column if not exists last_error text,
    add column if not exists has_partial_failures boolean default false not null;

update public.pipeline_runs
    set state_entered_at = coalesce(finished_at, started_at, now())
    where state_entered_at is null;

alter table public.pipeline_runs
    alter column state_entered_at set not null,
    alter column state_entered_at set default now();

-- ============================================================
-- profiles (onboarding substrate)
-- ============================================================
alter table public.profiles
    add column if not exists onboarding_state_entered_at timestamptz,
    add column if not exists onboarding_stage text,
    add column if not exists onboarding_stage_entered_at timestamptz,
    add column if not exists onboarding_failure_count integer default 0 not null,
    add column if not exists onboarding_last_error text;

update public.profiles
    set onboarding_state_entered_at = coalesce(onboarding_started_at, created_at, now())
    where onboarding_state_entered_at is null
      and onboarding_status is not null;

-- onboarding_state_entered_at stays nullable: NULL means onboarding has
-- never started for this profile. Not every profile has an active lifecycle.

commit;
