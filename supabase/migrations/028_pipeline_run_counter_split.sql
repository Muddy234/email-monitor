-- Add granular counters to pipeline_runs so we can distinguish
-- classification success from draft generation success.
-- emails_processed is kept as-is for backward compatibility.

ALTER TABLE pipeline_runs
  ADD COLUMN IF NOT EXISTS emails_classified integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS emails_drafted integer DEFAULT 0;

-- Backfill existing rows: emails_classified = emails_processed,
-- emails_drafted = drafts_generated (best approximation).
UPDATE pipeline_runs
SET emails_classified = COALESCE(emails_processed, 0),
    emails_drafted = COALESCE(drafts_generated, 0)
WHERE emails_classified = 0;
