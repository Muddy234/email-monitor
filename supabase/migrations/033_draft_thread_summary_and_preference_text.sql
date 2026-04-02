-- Add thread_summary column to drafts table
ALTER TABLE public.drafts
  ADD COLUMN IF NOT EXISTS thread_summary text DEFAULT NULL;

-- Convert preference_profile from jsonb to text
ALTER TABLE public.profiles
  ALTER COLUMN preference_profile TYPE text USING preference_profile::text;
