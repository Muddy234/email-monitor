-- Email Monitor: Initial Supabase schema
-- Tables, RLS policies, and RPC functions for the extension + worker architecture.

-- ============================================================
-- profiles — per-user configuration, linked to Supabase Auth
-- ============================================================
create table public.profiles (
    id uuid primary key references auth.users on delete cascade,
    email_provider_origin text default 'outlook.live.com',
    process_flagged_only boolean default false,
    max_emails_to_scan integer default 500,
    start_date timestamptz,
    user_email_aliases text[] default '{}',
    last_sync_at timestamptz,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

alter table public.profiles enable row level security;

create policy "Users can read own profile"
    on public.profiles for select using (auth.uid() = id);
create policy "Users can update own profile"
    on public.profiles for update using (auth.uid() = id);
create policy "Users can insert own profile"
    on public.profiles for insert with check (auth.uid() = id);

-- Auto-create profile on signup
create or replace function public.handle_new_user()
returns trigger as $$
begin
    insert into public.profiles (id, user_email_aliases, process_flagged_only)
    values (new.id, ARRAY[new.email], false);
    return new;
end;
$$ language plpgsql security definer;

create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();


-- ============================================================
-- emails — raw email data pushed by the extension
-- ============================================================
create table public.emails (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    email_ref text not null,
    subject text,
    sender text,
    sender_name text,
    sender_email text,
    received_time timestamptz,
    body text,
    has_attachments boolean default false,
    attachment_names text[] default '{}',
    folder text,
    flag_status text default 'NotFlagged',
    conversation_id text,
    conversation_topic text,
    to_field text default '',
    cc_field text default '',
    importance text default 'Normal',
    recipients jsonb default '[]',
    status text default 'unprocessed',
    created_at timestamptz default now(),

    constraint emails_user_ref_unique unique (user_id, email_ref)
);

create index idx_emails_status on public.emails (status);
create index idx_emails_user_status on public.emails (user_id, status);
create index idx_emails_conversation on public.emails (conversation_id);

alter table public.emails enable row level security;

create policy "Users can insert own emails"
    on public.emails for insert with check (auth.uid() = user_id);
create policy "Users can read own emails"
    on public.emails for select using (auth.uid() = user_id);


-- ============================================================
-- classifications — AI analysis results per email
-- ============================================================
create table public.classifications (
    id uuid primary key default gen_random_uuid(),
    email_id uuid not null references public.emails on delete cascade,
    user_id uuid not null references public.profiles on delete cascade,
    needs_response boolean default false,
    action text,
    context text,
    project text,
    priority integer default 0,
    created_at timestamptz default now()
);

create index idx_classifications_email on public.classifications (email_id);

alter table public.classifications enable row level security;

create policy "Users can read own classifications"
    on public.classifications for select using (auth.uid() = user_id);


-- ============================================================
-- drafts — generated draft responses
-- ============================================================
create table public.drafts (
    id uuid primary key default gen_random_uuid(),
    email_id uuid not null references public.emails on delete cascade,
    user_id uuid not null references public.profiles on delete cascade,
    draft_body text,
    status text default 'pending',
    outlook_draft_id text,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index idx_drafts_user_status on public.drafts (user_id, status);

alter table public.drafts enable row level security;

create policy "Users can read own drafts"
    on public.drafts for select using (auth.uid() = user_id);
create policy "Users can update own drafts"
    on public.drafts for update using (auth.uid() = user_id);


-- ============================================================
-- pipeline_runs — logging for worker executions
-- ============================================================
create table public.pipeline_runs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    trigger_type text default 'scheduled',
    status text default 'running',
    emails_scanned integer default 0,
    emails_processed integer default 0,
    drafts_generated integer default 0,
    log_output text,
    error_message text,
    started_at timestamptz default now(),
    finished_at timestamptz
);

alter table public.pipeline_runs enable row level security;

create policy "Users can read own pipeline runs"
    on public.pipeline_runs for select using (auth.uid() = user_id);


-- ============================================================
-- conversations — threaded context for draft quality
-- ============================================================
create table public.conversations (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles on delete cascade,
    conversation_id text not null,
    messages jsonb default '[]',
    updated_at timestamptz default now(),

    constraint conversations_user_convo_unique unique (user_id, conversation_id)
);

create index idx_conversations_lookup on public.conversations (user_id, conversation_id);

alter table public.conversations enable row level security;

create policy "Users can insert own conversations"
    on public.conversations for insert with check (auth.uid() = user_id);
create policy "Users can read own conversations"
    on public.conversations for select using (auth.uid() = user_id);
create policy "Users can update own conversations"
    on public.conversations for update using (auth.uid() = user_id);


-- ============================================================
-- Realtime — enable for drafts table (extension listens for
-- new drafts with status='pending' to write back to Outlook)
-- ============================================================
alter publication supabase_realtime add table public.drafts;


-- ============================================================
-- RPC: claim_unprocessed_emails
-- Atomic claim-and-process: sets status='processing' and returns
-- the claimed rows in one operation. Prevents duplicate processing
-- when poll intervals overlap with long-running Claude calls.
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
        where status = 'unprocessed'
          and user_id = p_user_id
        order by received_time asc
        limit p_limit
        for update skip locked
    )
    returning *;
end;
$$ language plpgsql security definer;
