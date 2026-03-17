-- Add summary column to response_events for storing LLM thinking/analysis.
-- Draft emails: stores Sonnet's <thinking> block extracted during draft generation.
-- Notable emails: stores Haiku-generated summary analysis.

ALTER TABLE response_events ADD COLUMN IF NOT EXISTS summary text;
