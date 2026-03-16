-- Add per-contact preference columns for VIP, draft override, and priority override
ALTER TABLE public.contacts
  ADD COLUMN IF NOT EXISTS is_vip boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS draft_preference text NOT NULL DEFAULT 'auto'
    CHECK (draft_preference IN ('always', 'never', 'auto')),
  ADD COLUMN IF NOT EXISTS priority_override text DEFAULT NULL
    CHECK (priority_override IS NULL OR priority_override IN ('high', 'med', 'low'));
