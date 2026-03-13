-- Signal extraction: add Haiku signal columns to response_events,
-- create domain_tiers lookup table, and seed initial domain tiers.

BEGIN;

-- New signal columns on response_events
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS mc boolean;
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS ar boolean;
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS ub boolean;
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS dl boolean;
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS rt text CHECK (rt IN ('none', 'ack', 'ans', 'act', 'dec'));
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS pri text CHECK (pri IN ('high', 'med', 'low'));
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS draft boolean;
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS reason text;
ALTER TABLE response_events ADD COLUMN IF NOT EXISTS sender_tier text CHECK (sender_tier IN ('C', 'I', 'P', 'U'));

-- Domain tiers lookup table (global, not per-user)
CREATE TABLE IF NOT EXISTS domain_tiers (
    domain text PRIMARY KEY,
    tier text NOT NULL CHECK (tier IN ('C', 'I', 'P', 'U')),
    source text DEFAULT 'seed',  -- 'seed', 'manual', 'derived'
    created_at timestamptz DEFAULT now()
);

-- Seed critical domains: lenders, title companies, legal, investors
INSERT INTO domain_tiers (domain, tier, source) VALUES
    -- Lenders
    ('zionsbank.com', 'C', 'seed'),
    ('zionsbancorp.com', 'C', 'seed'),
    ('wellsfargo.com', 'C', 'seed'),
    ('jpmorgan.com', 'C', 'seed'),
    ('jpmchase.com', 'C', 'seed'),
    ('chase.com', 'C', 'seed'),
    ('bankofamerica.com', 'C', 'seed'),
    ('usbank.com', 'C', 'seed'),
    ('pnc.com', 'C', 'seed'),
    ('keystonebank.com', 'C', 'seed'),
    ('citibank.com', 'C', 'seed'),
    ('regions.com', 'C', 'seed'),
    ('tdbank.com', 'C', 'seed'),
    ('bfrb.com', 'C', 'seed'),
    -- Title companies
    ('firstam.com', 'C', 'seed'),
    ('stewart.com', 'C', 'seed'),
    ('fidelitynationaltitle.com', 'C', 'seed'),
    ('oldrepublictitle.com', 'C', 'seed'),
    ('chicagotitle.com', 'C', 'seed'),
    -- Legal
    ('kirklandelliscom', 'C', 'seed'),
    ('dlapiper.com', 'C', 'seed'),
    ('hollandhart.com', 'C', 'seed'),
    ('stoel.com', 'C', 'seed'),
    -- Government / regulatory
    ('fdic.gov', 'C', 'seed'),
    ('sec.gov', 'C', 'seed'),
    ('sba.gov', 'C', 'seed'),
    -- Internal
    ('arete-collective.com', 'I', 'seed'),
    ('aretecollective.com', 'I', 'seed')
ON CONFLICT (domain) DO NOTHING;

-- Index for fast tier lookups
CREATE INDEX IF NOT EXISTS idx_domain_tiers_domain ON domain_tiers (domain);

-- RLS: domain_tiers is public read-only (no user_id column)
ALTER TABLE domain_tiers ENABLE ROW LEVEL SECURITY;
CREATE POLICY "domain_tiers_read" ON domain_tiers FOR SELECT USING (true);

COMMIT;
