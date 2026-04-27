ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS style_sample_email_count integer DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS behavioral_extracted_feature_count integer DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS behavioral_sample_email_count integer DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS preference_decision_count integer DEFAULT NULL;
