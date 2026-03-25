-- Convert domain_tiers from global to per-user table.
-- Single source of truth for default domains: _insert_seed_domain_tiers(uuid).

BEGIN;

-- 1. Add user_id column (nullable for now)
ALTER TABLE domain_tiers
    ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE;

-- 2a. Internal helper: single source of truth for seed domain list
CREATE OR REPLACE FUNCTION _insert_seed_domain_tiers(p_user_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    INSERT INTO domain_tiers (domain, tier, source, user_id) VALUES
        -- Lenders
        ('zionsbank.com',             'C', 'seed', p_user_id),
        ('zionsbancorp.com',          'C', 'seed', p_user_id),
        ('wellsfargo.com',            'C', 'seed', p_user_id),
        ('jpmorgan.com',              'C', 'seed', p_user_id),
        ('jpmchase.com',              'C', 'seed', p_user_id),
        ('chase.com',                 'C', 'seed', p_user_id),
        ('bankofamerica.com',         'C', 'seed', p_user_id),
        ('usbank.com',                'C', 'seed', p_user_id),
        ('pnc.com',                   'C', 'seed', p_user_id),
        ('keystonebank.com',          'C', 'seed', p_user_id),
        ('citibank.com',              'C', 'seed', p_user_id),
        ('regions.com',               'C', 'seed', p_user_id),
        ('tdbank.com',                'C', 'seed', p_user_id),
        ('bfrb.com',                  'C', 'seed', p_user_id),
        -- Title companies
        ('firstam.com',               'C', 'seed', p_user_id),
        ('stewart.com',               'C', 'seed', p_user_id),
        ('fidelitynationaltitle.com', 'C', 'seed', p_user_id),
        ('oldrepublictitle.com',      'C', 'seed', p_user_id),
        ('chicagotitle.com',          'C', 'seed', p_user_id),
        -- Legal
        ('kirklandelliscom',          'C', 'seed', p_user_id),
        ('dlapiper.com',              'C', 'seed', p_user_id),
        ('hollandhart.com',           'C', 'seed', p_user_id),
        ('stoel.com',                 'C', 'seed', p_user_id),
        -- Government / regulatory
        ('fdic.gov',                  'C', 'seed', p_user_id),
        ('sec.gov',                   'C', 'seed', p_user_id),
        ('sba.gov',                   'C', 'seed', p_user_id)
    ON CONFLICT DO NOTHING;
END;
$$;

-- 2b. Trigger function calls the helper
CREATE OR REPLACE FUNCTION seed_domain_tiers_for_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    PERFORM _insert_seed_domain_tiers(NEW.id);
    RETURN NEW;
END;
$$;

-- 3. Create trigger on profiles INSERT (fires after handle_new_user inserts the profile)
CREATE TRIGGER seed_domain_tiers_on_profile
    AFTER INSERT ON profiles
    FOR EACH ROW
    EXECUTE FUNCTION seed_domain_tiers_for_user();

-- 4. Delete old global rows FIRST (PK is still (domain), so backfill
--    would conflict with existing rows if we insert before deleting)
DELETE FROM domain_tiers WHERE user_id IS NULL;

-- 5. Backfill existing users via the same helper function
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT id FROM profiles LOOP
        PERFORM _insert_seed_domain_tiers(r.id);
    END LOOP;
END;
$$;

-- 6. Make user_id NOT NULL
ALTER TABLE domain_tiers ALTER COLUMN user_id SET NOT NULL;

-- 7. Replace PK: (domain) → (user_id, domain)
ALTER TABLE domain_tiers DROP CONSTRAINT domain_tiers_pkey;
ALTER TABLE domain_tiers ADD PRIMARY KEY (user_id, domain);

-- 8. Drop old global-read RLS policy
DROP POLICY IF EXISTS "domain_tiers_read" ON domain_tiers;

-- 9. Add user-scoped RLS policies
CREATE POLICY "domain_tiers_select_own" ON domain_tiers
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "domain_tiers_insert_own" ON domain_tiers
    FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "domain_tiers_update_own" ON domain_tiers
    FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY "domain_tiers_delete_own" ON domain_tiers
    FOR DELETE USING (auth.uid() = user_id);

COMMIT;
