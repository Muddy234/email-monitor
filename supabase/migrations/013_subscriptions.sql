-- Subscriptions table for Stripe billing integration.
-- Each user gets one row, auto-created on signup as 'inactive'.

CREATE TABLE subscriptions (
  id                      UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id                 UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  stripe_customer_id      TEXT,
  stripe_subscription_id  TEXT,
  status                  TEXT NOT NULL DEFAULT 'inactive',
    -- active, past_due, canceled, inactive, trialing
  plan                    TEXT NOT NULL DEFAULT 'pro',
  current_period_start    TIMESTAMPTZ,
  current_period_end      TIMESTAMPTZ,
  cancel_at_period_end    BOOLEAN DEFAULT false,
  created_at              TIMESTAMPTZ DEFAULT now(),
  updated_at              TIMESTAMPTZ DEFAULT now(),

  UNIQUE (user_id),
  UNIQUE (stripe_subscription_id)
);

-- RLS: users can only read their own subscription
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users read own subscription"
  ON subscriptions FOR SELECT
  USING (auth.uid() = user_id);

-- Auto-create an inactive subscription row when a new user signs up
CREATE OR REPLACE FUNCTION create_default_subscription()
RETURNS trigger AS $$
BEGIN
  INSERT INTO subscriptions (user_id, status)
  VALUES (NEW.id, 'inactive');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created_subscription
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION create_default_subscription();

-- Backfill: grant existing users 'active' status (grandfathered).
-- current_period_end is null — the dashboard shows "Grandfathered" for these.
INSERT INTO subscriptions (user_id, status, plan, current_period_start, current_period_end)
SELECT id, 'active', 'pro', now(), null
FROM auth.users
WHERE id NOT IN (SELECT user_id FROM subscriptions);
