-- Add initial_sync_complete flag for gating onboarding on extension sync completion.
-- Rename style_sample_count to reflect its new semantics (Change 9).

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS initial_sync_complete boolean DEFAULT false;

ALTER TABLE public.profiles
  RENAME COLUMN style_sample_count TO style_extracted_feature_count;
