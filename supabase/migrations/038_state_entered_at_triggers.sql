-- Phase 1 of state management refactor (part 2).
-- Installs BEFORE UPDATE triggers that maintain state_entered_at and
-- failure_count automatically on every status change.
--
-- Keeping failure_count in the trigger (not a helper function) ensures
-- every code path writing status='failed' gets counted, even ones we
-- haven't refactored yet or future direct SQL writes.
--
-- Trigger semantics:
--   * state_entered_at := now() on any status transition
--   * failure_count += 1 on transition INTO 'failed' from any other state
--   * failure_count := 0 on transition OUT OF 'failed' to a non-failed state
--
-- Phase 1 is additive: the triggers fire now, but current code still
-- writes legacy values. The trigger logic keys off the NEW unified values
-- ('failed'), so during Phase 1 before the data migration runs, the
-- failure_count branches won't fire (no row has status='failed' yet on
-- emails/drafts today). They start firing once Phase 3 unifies vocabulary.
-- This is the intended behavior.

begin;

-- ============================================================
-- Generic trigger: emails / drafts / pipeline_runs
-- ============================================================
create or replace function public.bump_state_entered_at()
returns trigger
language plpgsql
as $$
begin
    if new.status is distinct from old.status then
        new.state_entered_at := now();

        if new.status = 'failed' and old.status is distinct from 'failed' then
            new.failure_count := coalesce(old.failure_count, 0) + 1;
        elsif old.status = 'failed' and new.status <> 'failed' then
            new.failure_count := 0;
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists emails_bump_state on public.emails;
create trigger emails_bump_state
    before update on public.emails
    for each row
    execute function public.bump_state_entered_at();

drop trigger if exists drafts_bump_state on public.drafts;
create trigger drafts_bump_state
    before update on public.drafts
    for each row
    execute function public.bump_state_entered_at();

drop trigger if exists pipeline_runs_bump_state on public.pipeline_runs;
create trigger pipeline_runs_bump_state
    before update on public.pipeline_runs
    for each row
    execute function public.bump_state_entered_at();

-- ============================================================
-- Profiles-specific trigger (onboarding substrate)
-- ============================================================
create or replace function public.bump_onboarding_state_entered_at()
returns trigger
language plpgsql
as $$
begin
    if new.onboarding_status is distinct from old.onboarding_status then
        new.onboarding_state_entered_at := now();

        if new.onboarding_status = 'failed'
           and old.onboarding_status is distinct from 'failed' then
            new.onboarding_failure_count := coalesce(old.onboarding_failure_count, 0) + 1;
        elsif old.onboarding_status = 'failed'
              and new.onboarding_status <> 'failed' then
            new.onboarding_failure_count := 0;
        end if;
    end if;

    if new.onboarding_stage is distinct from old.onboarding_stage then
        new.onboarding_stage_entered_at := now();
    end if;

    return new;
end;
$$;

drop trigger if exists profiles_bump_onboarding_state on public.profiles;
create trigger profiles_bump_onboarding_state
    before update on public.profiles
    for each row
    execute function public.bump_onboarding_state_entered_at();

commit;
