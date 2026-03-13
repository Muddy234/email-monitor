-- Token usage tracking: daily rollup per user/model/stage
-- Enables per-day and inception-to-date cost reporting.

CREATE TABLE IF NOT EXISTS token_usage (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    model text NOT NULL,
    stage text NOT NULL,
    usage_date date NOT NULL DEFAULT CURRENT_DATE,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    cache_read_tokens bigint NOT NULL DEFAULT 0,
    cache_creation_tokens bigint NOT NULL DEFAULT 0,
    request_count int NOT NULL DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE (user_id, model, stage, usage_date)
);

-- Index for dashboard queries
CREATE INDEX IF NOT EXISTS idx_token_usage_user_date
    ON token_usage (user_id, usage_date DESC);

-- RLS: users can read their own usage
ALTER TABLE token_usage ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own token usage"
    ON token_usage FOR SELECT
    USING (auth.uid() = user_id);

-- Service role has full access (worker writes via service key)

-- Atomic upsert-with-increment function (avoids race conditions)
CREATE OR REPLACE FUNCTION increment_token_usage(
    p_user_id uuid,
    p_model text,
    p_stage text,
    p_usage_date date,
    p_input_tokens bigint,
    p_output_tokens bigint,
    p_cache_read_tokens bigint,
    p_cache_creation_tokens bigint
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    INSERT INTO token_usage (user_id, model, stage, usage_date,
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
        request_count)
    VALUES (p_user_id, p_model, p_stage, p_usage_date,
        p_input_tokens, p_output_tokens, p_cache_read_tokens, p_cache_creation_tokens,
        1)
    ON CONFLICT (user_id, model, stage, usage_date)
    DO UPDATE SET
        input_tokens = token_usage.input_tokens + EXCLUDED.input_tokens,
        output_tokens = token_usage.output_tokens + EXCLUDED.output_tokens,
        cache_read_tokens = token_usage.cache_read_tokens + EXCLUDED.cache_read_tokens,
        cache_creation_tokens = token_usage.cache_creation_tokens + EXCLUDED.cache_creation_tokens,
        request_count = token_usage.request_count + 1,
        updated_at = now();
END;
$$;

-- View: inception-to-date summary per user per model
CREATE OR REPLACE VIEW token_usage_summary AS
SELECT
    user_id,
    model,
    SUM(input_tokens) AS total_input_tokens,
    SUM(output_tokens) AS total_output_tokens,
    SUM(cache_read_tokens) AS total_cache_read_tokens,
    SUM(cache_creation_tokens) AS total_cache_creation_tokens,
    SUM(request_count) AS total_requests,
    -- Cost calculation (cents)
    -- Haiku: $0.80/1M input, $4/1M output, cache read 90% off, cache create 25% premium
    -- Sonnet: $3/1M input, $15/1M output, cache read 90% off, cache create 25% premium
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
