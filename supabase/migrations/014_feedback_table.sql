-- Feedback table: stores user corrections on email classifications/drafts
create table if not exists public.feedback (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    email_id uuid not null references public.emails(id) on delete cascade,
    feedback_type text not null check (feedback_type in ('positive', 'negative')),
    correction_category text check (correction_category in ('no_response_needed', 'response_needed', 'wrong_priority', 'draft_quality', 'other')),
    correction_value text,
    created_at timestamptz not null default now()
);

-- Index for querying feedback by email
create index if not exists idx_feedback_email_id on public.feedback(email_id);
create index if not exists idx_feedback_user_id on public.feedback(user_id);

-- RLS
alter table public.feedback enable row level security;

create policy "Users can insert their own feedback"
    on public.feedback for insert
    with check (auth.uid() = user_id);

create policy "Users can read their own feedback"
    on public.feedback for select
    using (auth.uid() = user_id);
