-- NOTE: Shares 003 prefix with 003_heartbeat_columns.sql. Both are idempotent (IF NOT EXISTS). Safe to run in any order.
-- Clarion AI: Onboarding schema
-- Tables and columns for the one-time onboarding intelligence build.
-- Run after 002_dashboard_policies.sql.

-- ============================================================
-- profiles — onboarding + style columns
-- ============================================================
alter table public.profiles
    add column if not exists onboarding_status text default null,
    add column if not exists onboarding_started_at timestamptz default null,
    add column if not exists onboarding_completed_at timestamptz default null,
    add column if not exists writing_style_guide text default null,
    add column if not exists style_profiled_at timestamptz default null,
    add column if not exists style_sample_count integer default 0;

-- Partial index for fast onboarding detection by worker
create index if not exists idx_profiles_onboarding
    on public.profiles (onboarding_completed_at)
    where onboarding_completed_at is null;


-- ============================================================
-- contacts — per-user contact directory built at onboarding
-- ============================================================
create table if not exists public.contacts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    email text not null,
    name text,
    organization text,
    role text,
    expertise_areas text[] default '{}',
    contact_type text default 'unknown',
    relationship_significance text default 'medium',
    relationship_summary text,
    emails_per_month integer default 0,
    response_rate numeric(5,2),
    avg_response_time_hours numeric(8,2),
    user_initiates_pct numeric(5,2),
    common_co_recipients text[] default '{}',
    last_interaction_at timestamptz,
    last_profiled_at timestamptz,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),

    constraint contacts_user_email_unique unique (user_id, email)
);

create index if not exists idx_contacts_user on public.contacts (user_id);
create index if not exists idx_contacts_lookup on public.contacts (user_id, email);

alter table public.contacts enable row level security;

create policy "Users can read own contacts"
    on public.contacts for select using (auth.uid() = user_id);


-- ============================================================
-- user_topic_profile — topic domains, keywords, calibration
-- ============================================================
create table if not exists public.user_topic_profile (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    domains jsonb not null default '[]',
    high_signal_keywords text[] not null default '{}',
    worked_examples jsonb default null,
    classification_rules jsonb default null,
    token_frequencies jsonb default null,
    baseline_statistics jsonb default '{}',
    created_at timestamptz default now(),
    updated_at timestamptz default now(),

    constraint user_topic_profile_unique unique (user_id)
);

alter table public.user_topic_profile enable row level security;

create policy "Users can read own topic profile"
    on public.user_topic_profile for select using (auth.uid() = user_id);
