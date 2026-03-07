-- Dashboard policies and schema additions
-- Run this in the Supabase SQL editor after 001_initial_schema.sql

-- Allow users to update their own emails (Mark Completed)
create policy "Users can update own emails"
  on public.emails for update using (auth.uid() = user_id);

-- Add user_edited flag to drafts (prevents worker overwrite)
alter table public.drafts
  add column if not exists user_edited boolean default false;

-- Client-side error logging
create table if not exists public.error_logs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references public.profiles on delete cascade,
    page text,
    error text,
    stack text,
    created_at timestamptz default now()
);

alter table public.error_logs enable row level security;

create policy "Users can insert own errors"
    on public.error_logs for insert with check (auth.uid() = user_id);
