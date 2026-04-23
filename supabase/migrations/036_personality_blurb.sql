-- Opus-aggregated personality blurb on user profiles
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS personality_blurb text,
  ADD COLUMN IF NOT EXISTS personality_blurbed_at timestamptz;

COMMENT ON COLUMN public.profiles.personality_blurb IS
  'Opus-aggregated blurb of style + behavioral + preference guides. Injected into draft prompts.';
