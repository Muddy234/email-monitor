-- NOTE: Shares 006 prefix with 006_trace_scoring_columns.sql. Both are idempotent.
-- Fix: Reset emails stuck in "processing" status back to "unprocessed"
-- so the worker can re-claim and process them.
--
-- Root cause: background.js upsert was overwriting status="unprocessed"
-- on every sync cycle, creating race conditions with the worker.
-- The extension fix (removing status from upsert payload) prevents
-- recurrence; this migration fixes the existing stuck data.

-- Reset all "processing" emails back to "unprocessed"
UPDATE public.emails
SET status = 'unprocessed'
WHERE status = 'processing';

-- Fix any stuck pipeline_runs that never completed
UPDATE public.pipeline_runs
SET status = 'failed',
    error_message = 'Reset: stuck in running state due to status-reset bug'
WHERE status = 'running';

-- Cap the soft_gate_threshold in scoring_parameters to 0.10
-- The model trainer bug allowed this to climb too high, gating nearly everything.
UPDATE public.scoring_parameters
SET parameters = jsonb_set(
    parameters,
    '{triage,soft_gate_threshold}',
    '0.10'
)
WHERE (parameters->'triage'->>'soft_gate_threshold')::numeric > 0.10;
