-- Bulk upsert contact stats in a single round-trip instead of N+1 updates.
-- Called from worker/supabase_client.py bulk_upsert_contact_stats().

CREATE OR REPLACE FUNCTION increment_contact_stats(
    p_user_id uuid,
    p_contacts jsonb  -- [{email, name, contact_type, received_time}, ...]
) RETURNS void AS $$
    INSERT INTO contacts (user_id, email, name, contact_type, total_received, last_interaction_at, updated_at)
    SELECT
        p_user_id,
        c->>'email',
        c->>'name',
        CASE WHEN c->>'contact_type' IS NOT NULL AND c->>'contact_type' != 'unknown'
             THEN c->>'contact_type'
             ELSE 'unknown'
        END,
        1,
        COALESCE((c->>'received_time')::timestamptz, now()),
        now()
    FROM jsonb_array_elements(p_contacts) AS c
    ON CONFLICT (user_id, email) DO UPDATE SET
        total_received = contacts.total_received + 1,
        name = COALESCE(EXCLUDED.name, contacts.name),
        contact_type = CASE
            WHEN EXCLUDED.contact_type != 'unknown' THEN EXCLUDED.contact_type
            ELSE contacts.contact_type
        END,
        last_interaction_at = GREATEST(contacts.last_interaction_at, EXCLUDED.last_interaction_at),
        updated_at = now();
$$ LANGUAGE sql SECURITY DEFINER;
