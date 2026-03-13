-- Add action target column to response_events for tracking
-- who an action/request is directed at (user, other, all, unclear).

ALTER TABLE response_events
ADD COLUMN IF NOT EXISTS target text
CHECK (target IN ('user', 'other', 'all', 'unclear'));
