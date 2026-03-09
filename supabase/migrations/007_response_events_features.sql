-- Add feature columns to response_events for full model training.
--
-- These columns were previously either hardcoded (mentions_user_name,
-- sender_is_internal) or missing entirely (thread_user_initiated,
-- arrived_during_active_hours, arrived_on_active_day, thread_depth)
-- in the model trainer, limiting the scorer to only 4 of 17 possible
-- multipliers.  Persisting them on each event lets the trainer learn
-- real lift values for every signal.
--
-- scoring_factors stores the human-readable factor list produced by
-- score_email() for observability / debugging.

ALTER TABLE public.response_events
  ADD COLUMN IF NOT EXISTS mentions_user_name        boolean DEFAULT false,
  ADD COLUMN IF NOT EXISTS sender_is_internal        boolean DEFAULT false,
  ADD COLUMN IF NOT EXISTS thread_user_initiated     boolean DEFAULT false,
  ADD COLUMN IF NOT EXISTS arrived_during_active_hours boolean,
  ADD COLUMN IF NOT EXISTS arrived_on_active_day     boolean,
  ADD COLUMN IF NOT EXISTS thread_depth              integer DEFAULT 1,
  ADD COLUMN IF NOT EXISTS scoring_factors           text[];
