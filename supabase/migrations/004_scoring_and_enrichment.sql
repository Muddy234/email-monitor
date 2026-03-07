-- Clarion AI: Scoring model, response events, threads, domains,
-- and enriched classification columns.
-- Run after 003_onboarding_schema.sql.

-- ============================================================
-- profiles — worker_active flag for explicit logout gating
-- ============================================================
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS worker_active boolean DEFAULT true;


-- ============================================================
-- emails — enriched classification fields written by worker
-- ============================================================
ALTER TABLE public.emails
  ADD COLUMN IF NOT EXISTS reason text,
  ADD COLUMN IF NOT EXISTS archetype text,
  ADD COLUMN IF NOT EXISTS classification_confidence numeric(5,4);


-- ============================================================
-- conversations — missing created_at column
-- ============================================================
ALTER TABLE public.conversations
  ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();


-- ============================================================
-- scoring_parameters — per-user scoring model artifacts
-- ============================================================
CREATE TABLE IF NOT EXISTS public.scoring_parameters (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.profiles ON DELETE CASCADE,
    parameters jsonb NOT NULL DEFAULT '{}',
    generated_at timestamptz DEFAULT now(),
    emails_used integer,
    created_at timestamptz DEFAULT now(),

    CONSTRAINT scoring_parameters_user_unique UNIQUE (user_id)
);

ALTER TABLE public.scoring_parameters ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own scoring parameters"
    ON public.scoring_parameters FOR SELECT USING (auth.uid() = user_id);


-- ============================================================
-- response_events — per-email response tracking for model training
-- ============================================================
CREATE TABLE IF NOT EXISTS public.response_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.profiles ON DELETE CASCADE,
    email_id text NOT NULL,
    sender_email text DEFAULT '',
    received_time timestamptz,
    responded boolean DEFAULT false,
    response_latency_hours numeric(10,2),
    response_type text,
    conversation_id text,
    subject text,
    user_position text,
    total_recipients integer,
    has_question boolean DEFAULT false,
    has_action_language boolean DEFAULT false,
    subject_type text,
    is_recurring boolean DEFAULT false,
    created_at timestamptz DEFAULT now(),

    CONSTRAINT response_events_user_email_unique UNIQUE (user_id, email_id)
);

CREATE INDEX IF NOT EXISTS idx_response_events_user
    ON public.response_events (user_id);
CREATE INDEX IF NOT EXISTS idx_response_events_user_created
    ON public.response_events (user_id, created_at);

ALTER TABLE public.response_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own response events"
    ON public.response_events FOR SELECT USING (auth.uid() = user_id);


-- ============================================================
-- threads — per-conversation thread statistics
-- ============================================================
CREATE TABLE IF NOT EXISTS public.threads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.profiles ON DELETE CASCADE,
    conversation_id text NOT NULL,
    total_messages integer DEFAULT 0,
    user_messages integer DEFAULT 0,
    participation_rate numeric(5,4),
    user_initiated boolean DEFAULT false,
    user_avg_body_length numeric(10,2),
    other_responders text[] DEFAULT '{}',
    duration_days numeric(10,2) DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),

    CONSTRAINT threads_user_convo_unique UNIQUE (user_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_threads_user
    ON public.threads (user_id);

ALTER TABLE public.threads ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own threads"
    ON public.threads FOR SELECT USING (auth.uid() = user_id);


-- ============================================================
-- domains — per-domain statistics for sender context
-- ============================================================
CREATE TABLE IF NOT EXISTS public.domains (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.profiles ON DELETE CASCADE,
    domain text NOT NULL,
    avg_reply_rate numeric(5,4),
    contact_count integer DEFAULT 0,
    domain_category text DEFAULT 'external',
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),

    CONSTRAINT domains_user_domain_unique UNIQUE (user_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_domains_user
    ON public.domains (user_id);

ALTER TABLE public.domains ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own domains"
    ON public.domains FOR SELECT USING (auth.uid() = user_id);
