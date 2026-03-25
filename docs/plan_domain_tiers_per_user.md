# Plan: Convert `domain_tiers` to Per-User Table

## Problem
`domain_tiers` is the only table without a `user_id` column. All users share the same domain classifications, including Nate's internal domains (`arete-collective.com`). This violates the requirement that all profile data be fully isolated between users.

## Design Decisions

### Single source of truth for seed domains
The default domain list lives in **one place**: the `seed_domain_tiers_for_user()` trigger function. The migration's backfill for existing users calls this same function rather than duplicating the list. This means adding/removing default domains later only requires updating the function.

### Transaction safety
The entire migration is wrapped in an explicit `BEGIN`/`COMMIT` block. Supabase migrations run in a transaction by default, but this makes it explicit — if any step fails, everything rolls back cleanly.

### Race condition prevention
The trigger and function are created **before** deleting global rows. If a user signs up mid-migration, they'll get seeded by the new trigger rather than falling through a gap.

### RLS scope
Full CRUD policies (SELECT, INSERT, UPDATE, DELETE) scoped to `auth.uid() = user_id`. Even though user-managed domain tiers aren't in scope yet, the policies are cheap and avoid a future migration.

## Changes

### 1. New migration: `supabase/migrations/020_domain_tiers_per_user.sql`

Execution order within a single transaction:

1. Add `user_id` column (nullable initially, FK to `auth.users ON DELETE CASCADE`)
2. Create `seed_domain_tiers_for_user()` trigger function (single source of default domains)
3. Create trigger on `profiles` INSERT → fires the seed function
4. Backfill existing users by calling the seed function for each profile
5. Delete old global rows (`WHERE user_id IS NULL`)
6. Make `user_id NOT NULL`
7. Replace PK: `(domain)` → `(user_id, domain)`
8. Drop old global-read RLS policy
9. Add user-scoped SELECT, INSERT, UPDATE, DELETE policies

Trigger chain: `auth.users INSERT` → `handle_new_user()` → `profiles INSERT` → `seed_domain_tiers_for_user()`

Excluded from seed list: `arete-collective.com`, `aretecollective.com`

### 2. Update `worker/supabase_client.py` (line ~825)
- `fetch_domain_tiers()` → `fetch_domain_tiers(self, user_id)`
- Add `.eq("user_id", user_id)` to the query

### 3. Update `worker/run_pipeline.py` (lines ~678-695, ~781)
- Convert module-level cache from flat dict to per-user dict: `{user_id: {domain: tier}}`
- `_get_domain_tiers(db)` → `_get_domain_tiers(db, user_id)`
- Update call site (line ~781) to pass `user_id`

### No changes needed
- `worker/pipeline/pre_process.py` — `resolve_sender_tier()` takes the cache as a plain dict param, unchanged
- Extension / web dashboard — don't touch `domain_tiers`

## Deployment Sequence

The migration changes the PK and removes global rows. The current worker code queries without a `user_id` filter. If the migration lands before the worker update, the pipeline will return wrong/duplicate results for the brief window in between.

**Recommended sequence:**
1. Push worker code to Railway (new code tolerates both schemas — it just adds `.eq("user_id", user_id)` which works on old schema too since user_id column exists after step 1)
2. Run `supabase db push` to apply migration
3. Railway auto-redeploys; verify logs

Given low user volume, the risk of a brief overlap is minimal. But deploying worker first is the safer order since the `.eq()` filter is additive.

## Rollback Plan

If something goes wrong post-deploy, run this manually in the Supabase SQL editor:

```sql
BEGIN;

-- Drop new trigger and function
DROP TRIGGER IF EXISTS seed_domain_tiers_on_profile ON profiles;
DROP FUNCTION IF EXISTS seed_domain_tiers_for_user();

-- Drop new RLS policies
DROP POLICY IF EXISTS "domain_tiers_select_own" ON domain_tiers;
DROP POLICY IF EXISTS "domain_tiers_insert_own" ON domain_tiers;
DROP POLICY IF EXISTS "domain_tiers_update_own" ON domain_tiers;
DROP POLICY IF EXISTS "domain_tiers_delete_own" ON domain_tiers;

-- Restore PK
ALTER TABLE domain_tiers DROP CONSTRAINT domain_tiers_pkey;
ALTER TABLE domain_tiers ALTER COLUMN user_id DROP NOT NULL;

-- Re-insert global seed rows (from original migration 010 list, minus arete)
INSERT INTO domain_tiers (domain, tier, source) VALUES
    ('zionsbank.com', 'C', 'seed'),
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
    ('zionsbancorp.com', 'C', 'seed'),
    ('firstam.com', 'C', 'seed'),
    ('stewart.com', 'C', 'seed'),
    ('fidelitynationaltitle.com', 'C', 'seed'),
    ('oldrepublictitle.com', 'C', 'seed'),
    ('chicagotitle.com', 'C', 'seed'),
    ('kirklandelliscom', 'C', 'seed'),
    ('dlapiper.com', 'C', 'seed'),
    ('hollandhart.com', 'C', 'seed'),
    ('stoel.com', 'C', 'seed'),
    ('fdic.gov', 'C', 'seed'),
    ('sec.gov', 'C', 'seed'),
    ('sba.gov', 'C', 'seed')
ON CONFLICT DO NOTHING;

-- Delete per-user rows
DELETE FROM domain_tiers WHERE user_id IS NOT NULL;

-- Drop user_id column
ALTER TABLE domain_tiers DROP COLUMN user_id;

-- Restore original PK and policy
ALTER TABLE domain_tiers ADD PRIMARY KEY (domain);
CREATE POLICY "domain_tiers_read" ON domain_tiers FOR SELECT USING (true);

COMMIT;
```

Then revert the worker code (`supabase_client.py` and `run_pipeline.py`) and redeploy.

## Verification

1. Run `supabase db push` to apply migration
2. Supabase SQL editor: `SELECT user_id, count(*) FROM domain_tiers GROUP BY user_id` — rows only for existing user(s), no NULL user_ids
3. Verify arete domains gone: `SELECT * FROM domain_tiers WHERE domain LIKE '%arete%'` — 0 rows
4. Deploy worker to Railway, monitor logs for successful pipeline runs
5. Confirm domain tier count in logs matches expected per-user count (~26 seed domains)
6. **New-user signup test:** Create a test user (via the app login flow or Supabase auth dashboard), then verify: `SELECT count(*) FROM domain_tiers WHERE user_id = '<test_user_id>'` — should return ~26 rows
7. Clean up test user if needed

## Files to Modify
- `supabase/migrations/020_domain_tiers_per_user.sql` — **new file**
- `worker/supabase_client.py` — line ~825, add `user_id` param + `.eq()` filter
- `worker/run_pipeline.py` — lines ~678-695 (cache) + line ~781 (call site)
