# Plan: Convert `domain_tiers` to Per-User Table

## Problem
`domain_tiers` is the only table without a `user_id` column. All users share the same domain classifications, including Nate's internal domains (`arete-collective.com`). This violates the requirement that all profile data be fully isolated between users.

## Changes

### 1. New migration: `supabase/migrations/020_domain_tiers_per_user.sql`
- Add `user_id` column (FK to `auth.users`, `ON DELETE CASCADE`)
- Migrate existing seed data → per-user rows for each existing user (minus arete domains)
- Delete global rows (where `user_id IS NULL`)
- Make `user_id NOT NULL`
- Replace PK: `(domain)` → `(user_id, domain)`
- Drop old global-read RLS policy, add user-scoped SELECT policy
- Create trigger function `seed_domain_tiers_for_user()` on `profiles` INSERT to seed default domains for new signups
- Trigger chains: `auth.users INSERT` → `handle_new_user()` → `profiles INSERT` → `seed_domain_tiers_for_user()`

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

## Verification
1. Run `supabase db push` to apply migration
2. Verify in Supabase SQL editor: `SELECT user_id, count(*) FROM domain_tiers GROUP BY user_id` — should show rows only for existing user(s), no NULL user_ids
3. Verify arete domains gone: `SELECT * FROM domain_tiers WHERE domain LIKE '%arete%'` — should return 0 rows
4. Deploy worker to Railway, monitor logs for successful pipeline runs
5. Confirm domain tier count in logs matches expected per-user count (~26 seed domains)
