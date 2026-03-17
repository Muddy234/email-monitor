-- Migration 014: Add trial_ends_at column and update signup trigger for 7-day free trial.
--
-- New users now start with status='trialing' and trial_ends_at = 7 days from signup.
-- Existing users are unaffected (trial_ends_at stays NULL).

ALTER TABLE public.subscriptions
  ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;

-- Update signup trigger: new users start with 7-day trial
CREATE OR REPLACE FUNCTION create_default_subscription()
RETURNS trigger AS $$
BEGIN
  INSERT INTO subscriptions (user_id, status, trial_ends_at)
  VALUES (NEW.id, 'trialing', now() + interval '7 days');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
