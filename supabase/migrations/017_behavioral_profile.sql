-- Behavioral profile on user profiles
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS behavioral_profile text,
  ADD COLUMN IF NOT EXISTS behavioral_profiled_at timestamptz;

-- Feedback signals on drafts
ALTER TABLE public.drafts
  ADD COLUMN IF NOT EXISTS edit_distance_ratio real,
  ADD COLUMN IF NOT EXISTS draft_deleted boolean DEFAULT false;

COMMENT ON COLUMN public.drafts.edit_distance_ratio IS
  'Ratio of characters changed between AI draft and user-sent version. Noisy metric — requires volume.';
COMMENT ON COLUMN public.drafts.draft_deleted IS
  'True if user discarded the draft entirely rather than editing. Stronger signal than edit distance.';
