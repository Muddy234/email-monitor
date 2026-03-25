-- Fill in all missing RLS policies for complete defense-in-depth.
-- Every table gets full CRUD policies scoped to auth.uid() = user_id.
-- Uses DROP IF EXISTS + CREATE to be safely re-runnable.

BEGIN;

-- ── error_logs (missing SELECT, UPDATE, DELETE) ──────────────────────
DROP POLICY IF EXISTS "error_logs_select_own" ON error_logs;
CREATE POLICY "error_logs_select_own" ON error_logs
    FOR SELECT USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "error_logs_update_own" ON error_logs;
CREATE POLICY "error_logs_update_own" ON error_logs
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "error_logs_delete_own" ON error_logs;
CREATE POLICY "error_logs_delete_own" ON error_logs
    FOR DELETE USING (auth.uid() = user_id);

-- ── classifications (missing INSERT, UPDATE, DELETE) ─────────────────
DROP POLICY IF EXISTS "classifications_insert_own" ON classifications;
CREATE POLICY "classifications_insert_own" ON classifications
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "classifications_update_own" ON classifications;
CREATE POLICY "classifications_update_own" ON classifications
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "classifications_delete_own" ON classifications;
CREATE POLICY "classifications_delete_own" ON classifications
    FOR DELETE USING (auth.uid() = user_id);

-- ── contacts (missing INSERT, UPDATE, DELETE) ────────────────────────
DROP POLICY IF EXISTS "contacts_insert_own" ON contacts;
CREATE POLICY "contacts_insert_own" ON contacts
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "contacts_update_own" ON contacts;
CREATE POLICY "contacts_update_own" ON contacts
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "contacts_delete_own" ON contacts;
CREATE POLICY "contacts_delete_own" ON contacts
    FOR DELETE USING (auth.uid() = user_id);

-- ── user_topic_profile (missing INSERT, UPDATE, DELETE) ──────────────
DROP POLICY IF EXISTS "user_topic_profile_insert_own" ON user_topic_profile;
CREATE POLICY "user_topic_profile_insert_own" ON user_topic_profile
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "user_topic_profile_update_own" ON user_topic_profile;
CREATE POLICY "user_topic_profile_update_own" ON user_topic_profile
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "user_topic_profile_delete_own" ON user_topic_profile;
CREATE POLICY "user_topic_profile_delete_own" ON user_topic_profile
    FOR DELETE USING (auth.uid() = user_id);

-- ── threads (missing INSERT, UPDATE, DELETE) ─────────────────────────
DROP POLICY IF EXISTS "threads_insert_own" ON threads;
CREATE POLICY "threads_insert_own" ON threads
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "threads_update_own" ON threads;
CREATE POLICY "threads_update_own" ON threads
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "threads_delete_own" ON threads;
CREATE POLICY "threads_delete_own" ON threads
    FOR DELETE USING (auth.uid() = user_id);

-- ── domains (missing INSERT, UPDATE, DELETE) ─────────────────────────
DROP POLICY IF EXISTS "domains_insert_own" ON domains;
CREATE POLICY "domains_insert_own" ON domains
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "domains_update_own" ON domains;
CREATE POLICY "domains_update_own" ON domains
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "domains_delete_own" ON domains;
CREATE POLICY "domains_delete_own" ON domains
    FOR DELETE USING (auth.uid() = user_id);

-- ── scoring_parameters (missing INSERT, UPDATE, DELETE) ──────────────
DROP POLICY IF EXISTS "scoring_parameters_insert_own" ON scoring_parameters;
CREATE POLICY "scoring_parameters_insert_own" ON scoring_parameters
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "scoring_parameters_update_own" ON scoring_parameters;
CREATE POLICY "scoring_parameters_update_own" ON scoring_parameters
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "scoring_parameters_delete_own" ON scoring_parameters;
CREATE POLICY "scoring_parameters_delete_own" ON scoring_parameters
    FOR DELETE USING (auth.uid() = user_id);

-- ── response_events (missing INSERT, UPDATE, DELETE) ─────────────────
DROP POLICY IF EXISTS "response_events_insert_own" ON response_events;
CREATE POLICY "response_events_insert_own" ON response_events
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "response_events_update_own" ON response_events;
CREATE POLICY "response_events_update_own" ON response_events
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "response_events_delete_own" ON response_events;
CREATE POLICY "response_events_delete_own" ON response_events
    FOR DELETE USING (auth.uid() = user_id);

-- ── token_usage (missing INSERT, UPDATE, DELETE) ─────────────────────
DROP POLICY IF EXISTS "token_usage_insert_own" ON token_usage;
CREATE POLICY "token_usage_insert_own" ON token_usage
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "token_usage_update_own" ON token_usage;
CREATE POLICY "token_usage_update_own" ON token_usage
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "token_usage_delete_own" ON token_usage;
CREATE POLICY "token_usage_delete_own" ON token_usage
    FOR DELETE USING (auth.uid() = user_id);

-- ── subscriptions (missing INSERT, UPDATE, DELETE) ───────────────────
DROP POLICY IF EXISTS "subscriptions_insert_own" ON subscriptions;
CREATE POLICY "subscriptions_insert_own" ON subscriptions
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "subscriptions_update_own" ON subscriptions;
CREATE POLICY "subscriptions_update_own" ON subscriptions
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "subscriptions_delete_own" ON subscriptions;
CREATE POLICY "subscriptions_delete_own" ON subscriptions
    FOR DELETE USING (auth.uid() = user_id);

-- ── feedback — table does not exist in DB yet (migration 014 never ran)
-- Policies will be added when the table is created.

-- ── drafts (missing INSERT, DELETE) ──────────────────────────────────
DROP POLICY IF EXISTS "drafts_insert_own" ON drafts;
CREATE POLICY "drafts_insert_own" ON drafts
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "drafts_delete_own" ON drafts;
CREATE POLICY "drafts_delete_own" ON drafts
    FOR DELETE USING (auth.uid() = user_id);

-- ── pipeline_runs (missing INSERT, UPDATE, DELETE) ───────────────────
DROP POLICY IF EXISTS "pipeline_runs_insert_own" ON pipeline_runs;
CREATE POLICY "pipeline_runs_insert_own" ON pipeline_runs
    FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "pipeline_runs_update_own" ON pipeline_runs;
CREATE POLICY "pipeline_runs_update_own" ON pipeline_runs
    FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "pipeline_runs_delete_own" ON pipeline_runs;
CREATE POLICY "pipeline_runs_delete_own" ON pipeline_runs
    FOR DELETE USING (auth.uid() = user_id);

-- ── profiles (missing DELETE; PK is id, not user_id) ─────────────────
DROP POLICY IF EXISTS "profiles_delete_own" ON profiles;
CREATE POLICY "profiles_delete_own" ON profiles
    FOR DELETE USING (auth.uid() = id);

-- ── emails (missing DELETE) ──────────────────────────────────────────
DROP POLICY IF EXISTS "emails_delete_own" ON emails;
CREATE POLICY "emails_delete_own" ON emails
    FOR DELETE USING (auth.uid() = user_id);

-- ── conversations (missing DELETE) ───────────────────────────────────
DROP POLICY IF EXISTS "conversations_delete_own" ON conversations;
CREATE POLICY "conversations_delete_own" ON conversations
    FOR DELETE USING (auth.uid() = user_id);

COMMIT;
