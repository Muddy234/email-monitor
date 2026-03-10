-- Add total_received column to contacts table.
--
-- The scorer uses total_received to weight Bayesian smoothing of each
-- sender's response_rate.  Without it, n_emails is always 0 and every
-- sender falls back to the global rate — making per-sender response
-- history useless.

ALTER TABLE public.contacts
  ADD COLUMN IF NOT EXISTS total_received integer DEFAULT 0;

-- Backfill from response_events: count emails per sender per user.
UPDATE public.contacts c
SET total_received = sub.cnt
FROM (
    SELECT user_id, sender_email, COUNT(*) AS cnt
    FROM public.response_events
    GROUP BY user_id, sender_email
) sub
WHERE c.user_id = sub.user_id
  AND c.email   = sub.sender_email;
