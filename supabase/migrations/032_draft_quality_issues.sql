-- Add quality control columns to drafts table
ALTER TABLE public.drafts
  ADD COLUMN IF NOT EXISTS quality_issues jsonb DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS quality_retry_count integer DEFAULT 0;
