-- Add preference profile column to profiles
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS preference_profile jsonb DEFAULT NULL;

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS preference_profiled_at timestamptz DEFAULT NULL;
