-- Add is_read column to emails table.
-- Defaults to true (matching OWA's IsRead default for older synced rows).
ALTER TABLE public.emails
  ADD COLUMN IF NOT EXISTS is_read boolean NOT NULL DEFAULT true;
