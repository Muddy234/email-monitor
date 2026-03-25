-- Fix: token_usage_summary view was running with creator's permissions,
-- bypassing RLS on token_usage. Recreate with security_invoker = true
-- so the querying user's RLS policies are enforced.

CREATE OR REPLACE VIEW token_usage_summary
WITH (security_invoker = true)
AS
SELECT
    user_id,
    model,
    SUM(input_tokens) AS total_input_tokens,
    SUM(output_tokens) AS total_output_tokens,
    SUM(cache_read_tokens) AS total_cache_read_tokens,
    SUM(cache_creation_tokens) AS total_cache_creation_tokens,
    SUM(request_count) AS total_requests,
    CASE model
        WHEN 'haiku' THEN ROUND((
            SUM(input_tokens) * 0.80 / 1000000.0
            + SUM(output_tokens) * 4.0 / 1000000.0
            + SUM(cache_read_tokens) * 0.08 / 1000000.0
            + SUM(cache_creation_tokens) * 1.0 / 1000000.0
        )::numeric, 4)
        WHEN 'sonnet' THEN ROUND((
            SUM(input_tokens) * 3.0 / 1000000.0
            + SUM(output_tokens) * 15.0 / 1000000.0
            + SUM(cache_read_tokens) * 0.30 / 1000000.0
            + SUM(cache_creation_tokens) * 3.75 / 1000000.0
        )::numeric, 4)
        ELSE 0
    END AS estimated_cost_usd,
    MIN(usage_date) AS first_usage_date,
    MAX(usage_date) AS last_usage_date
FROM token_usage
GROUP BY user_id, model;
