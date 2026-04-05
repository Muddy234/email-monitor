-- Add calibration columns to profiles table
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS calibration_status text DEFAULT 'pending'
    CHECK (calibration_status IN ('pending', 'running', 'passed', 'needs_review')),
  ADD COLUMN IF NOT EXISTS calibration_rules text DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS calibration_iteration integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS calibration_retry_count integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS calibrated_at timestamptz DEFAULT NULL;

-- Create calibration_results table for auditability
CREATE TABLE IF NOT EXISTS public.calibration_results (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  iteration integer NOT NULL,
  email_id text NOT NULL,
  ground_truth jsonb NOT NULL,
  generated_draft text,
  actual_reply text,
  incoming_email_body text,
  thread_summary text,
  style_delta jsonb NOT NULL,
  behavioral_delta jsonb NOT NULL,
  preference_delta jsonb NOT NULL,
  contextual_scores jsonb NOT NULL,
  overall_result text NOT NULL CHECK (overall_result IN ('pass', 'soft_miss', 'hard_miss')),
  correction_rules_applied text[],
  created_at timestamptz DEFAULT now()
);

-- Index for querying by user and iteration
CREATE INDEX IF NOT EXISTS idx_calibration_results_user_iteration
  ON public.calibration_results(user_id, iteration);

-- RLS policies
ALTER TABLE public.calibration_results ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own calibration results"
  ON public.calibration_results FOR SELECT
  USING (auth.uid() = user_id);

-- Service role has full access via service_role key (bypasses RLS)
