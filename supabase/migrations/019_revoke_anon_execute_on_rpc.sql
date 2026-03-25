-- Revoke public/anon access to worker-only RPC functions.
-- These are SECURITY DEFINER functions meant to be called only by
-- the service role (worker). No reason to expose them via PostgREST.

REVOKE EXECUTE ON FUNCTION increment_token_usage(uuid, text, text, date, bigint, bigint, bigint, bigint) FROM anon, authenticated;

REVOKE EXECUTE ON FUNCTION claim_unprocessed_emails(uuid, integer) FROM anon, authenticated;
