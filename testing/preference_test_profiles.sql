-- Hardcoded preference profiles for Step 1 validation
-- Test user: f0fe5970-dbe7-4ed2-b263-6431ba590111
--
-- Run ONE profile at a time, then regenerate drafts via backfill_drafts.py
-- to compare how the same 5 test emails produce different drafts.
--
-- Usage:
--   supabase db execute --linked < testing/preference_test_profiles.sql
--   (or copy/paste individual UPDATE blocks into Supabase SQL Editor)

-- ============================================================
-- PROFILE A: Invest Heavy + Advance Heavy (maximally assertive)
-- ============================================================

/*
UPDATE public.profiles
SET preference_profile = '{
  "investment_orientation": {
    "category": "invest_heavy",
    "description": "This user defaults to investing. They consistently shop vendors, close coverage gaps, fix problems fully, investigate root causes, explore alternatives, and act preemptively. When they see a gap between current state and better state, they act to close it — even in low-stakes situations where most people would accept good-enough. They do not wait for problems to force action.",
    "confidence": "high",
    "supporting_decisions": 22
  },
  "positional_stance": {
    "category": "advance_heavy",
    "description": "This user defaults to pushing. They negotiate concessions, demand reciprocity when yielding, exploit situational leverage, escalate commitment when competing, and pressure-test expert recommendations. They challenge the easy path when a harder path offers more control over outcomes. They do this consistently, including in low-stakes situations where most people would accommodate.",
    "confidence": "high",
    "supporting_decisions": 18
  }
}'::jsonb,
    preference_profiled_at = NOW()
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/

-- ============================================================
-- PROFILE B: Conserve Light + Yield Light (maximally deferential)
-- ============================================================

/*
UPDATE public.profiles
SET preference_profile = '{
  "investment_orientation": {
    "category": "conserve_light",
    "description": "This user leans toward conservation but is not passive. They accept the status quo on most decisions — renew without shopping, skip optional protections, take the partial fix — but will invest when a problem is urgent or the cost of inaction is obvious. Their default is inaction, but they respond when stakes are high.",
    "confidence": "high",
    "supporting_decisions": 16
  },
  "positional_stance": {
    "category": "yield_light",
    "description": "This user leans toward accommodation but is not a pushover. They generally follow expert guidance, concede without demanding reciprocity, and prefer collaborative resolution. On high-stakes matters where yielding has clear and obvious cost, they may hold ground or negotiate. Their default is to accommodate, but they push when clearly necessary.",
    "confidence": "high",
    "supporting_decisions": 15
  }
}'::jsonb,
    preference_profiled_at = NOW()
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/

-- ============================================================
-- PROFILE C: Invest Heavy + Yield Light (mixed — invests but accommodates)
-- ============================================================

/*
UPDATE public.profiles
SET preference_profile = '{
  "investment_orientation": {
    "category": "invest_heavy",
    "description": "This user defaults to investing. They consistently shop vendors, close coverage gaps, fix problems fully, investigate root causes, explore alternatives, and act preemptively. When they see a gap between current state and better state, they act to close it — even in low-stakes situations where most people would accept good-enough.",
    "confidence": "high",
    "supporting_decisions": 20
  },
  "positional_stance": {
    "category": "yield_light",
    "description": "This user leans toward accommodation. They generally follow expert guidance, concede without demanding reciprocity, and prefer collaborative resolution. On high-stakes matters where yielding has clear and obvious cost, they may hold ground. Their default is to accommodate.",
    "confidence": "high",
    "supporting_decisions": 12
  }
}'::jsonb,
    preference_profiled_at = NOW()
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/

-- ============================================================
-- RESET: Clear preference profile
-- ============================================================

/*
UPDATE public.profiles
SET preference_profile = NULL,
    preference_profiled_at = NULL
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/
