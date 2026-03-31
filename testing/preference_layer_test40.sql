-- ============================================================
-- PREFERENCE LAYER VALIDATION — Test 40 Re-runs
-- Email: Prof Services / Q1 Tax Filing
-- Constant: Style B (Professional) + Behavior 1 (High Authority)
-- Variable: Preference profile (3 configurations)
-- ============================================================
--
-- WORKFLOW (per configuration):
--   1. Uncomment the desired CONFIG block
--   2. Run this script (migration + profile update + email insert)
--   3. Worker picks up the new email on its next polling cycle
--   4. Run the VERIFY query to pull the generated draft
--
-- ============================================================


-- ============================================================
-- STEP 0: MIGRATION (run once, safe to re-run)
-- ============================================================

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS preference_profile jsonb DEFAULT NULL;

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS preference_profiled_at timestamptz DEFAULT NULL;


-- ============================================================
-- STEP 1: SET PROFILE — Style B + Behavior 1 + Preference Config
-- ============================================================
-- Style and behavior are constant across all configs.
-- Uncomment ONE config block at a time.

-- CONFIG 1: INVEST HEAVY + ADVANCE HEAVY
-- Expected draft direction:
--   - Q1 deliverables: Decide firmly (no Q1 work completed)
--   - Tax timing: Defer $38K to Q2 (invest in accurate accounting)
--   - Adjust books: Now (invest in clean books immediately)
--   - Positional: Independently evaluate Bobby's recommendation,
--     possibly push back or add conditions
--   - [USER TO CONFIRM] count: 0 on decisions

UPDATE profiles
SET writing_style_guide = '- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items',

    behavioral_profile = '- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date',

    preference_profile = '{
  "investment_orientation": {
    "category": "invest_heavy",
    "description": "This user invests by default across all decision types, including low-stakes items where most people would accept good-enough. They shop alternatives rather than accepting renewals, close gaps proactively, fix problems fully rather than patching, investigate root causes, and act preemptively. Their reasoning is action-oriented — when they see a gap between current state and better state, they move to close it without waiting for the problem to force their hand.",
    "confidence": "high",
    "supporting_decisions": 22
  },
  "positional_stance": {
    "category": "advance_heavy",
    "description": "This user pushes by default. They negotiate concessions, demand reciprocity when yielding ground, pressure-test expert recommendations rather than accepting them at face value, and exploit situational leverage. They challenge the easy path when a harder path offers more control over outcomes, even on lower-stakes interactions where most people would accommodate.",
    "confidence": "high",
    "supporting_decisions": 16
  }
}',

    preference_profiled_at = NOW()

WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';


-- CONFIG 2: CONSERVE LIGHT + YIELD LIGHT
-- Expected draft direction:
--   - Q1 deliverables: May defer to Bobby's assessment
--   - Tax timing: Keep as-is / pay now (conserve — avoid rework)
--   - Adjust books: Wait for full Q2 picture (conserve)
--   - Positional: Follow Bobby's recommendation without pushback
--   - [USER TO CONFIRM] count: Higher — defers more to expert

/*
UPDATE profiles
SET writing_style_guide = '- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items',

    behavioral_profile = '- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date',

    preference_profile = '{
  "investment_orientation": {
    "category": "conserve_light",
    "description": "This user defaults to the status quo on most decisions. They accept renewals without shopping, skip optional protections, and take partial fixes rather than investing in full remedies. When a problem is urgent or the cost of inaction is obvious, they will invest — but their default is conservation. Their reasoning is effort-driven: they weigh the hassle of acting against the cost of not acting, and the hassle usually wins unless stakes are clearly high.",
    "confidence": "high",
    "supporting_decisions": 18
  },
  "positional_stance": {
    "category": "yield_light",
    "description": "This user accommodates by default. They follow expert guidance without extensive independent evaluation, concede without demanding reciprocity, and prefer collaborative resolution over confrontation. On high-stakes matters where the cost of yielding is clear and obvious, they will hold ground — but their default is to go along with the recommended path.",
    "confidence": "high",
    "supporting_decisions": 12
  }
}',

    preference_profiled_at = NOW()

WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/


-- CONFIG 3: INVEST HEAVY + YIELD LIGHT (mixed)
-- Expected draft direction:
--   - Q1 deliverables: Decide firmly (invest — get it right)
--   - Tax timing: Defer $38K (invest in accurate accounting)
--   - Adjust books: Now (invest — clean books immediately)
--   - Positional: Follow Bobby's recommendation on methodology
--     (yield) but still commit to the investment direction
--   - Key difference from Config 1: accepts Bobby's framing
--     without challenging it; agrees rather than pressure-tests

/*
UPDATE profiles
SET writing_style_guide = '- Tone: formal, direct, business-like
- Pleasantries: minimal — brief greeting, no small talk
- Greeting pattern: "[First Name]," or "Good morning/afternoon,"
- Sign-off pattern: "Best," or "Regards,"
- Sentence structure: short declarative sentences, no filler
- Formality: high — no contractions, no exclamation marks, title + last name for new contacts
- Response length: concise — key points only, 40-80 words typical
- Verbal habits: "Understood.", "Will do.", "Please advise.", "Confirmed."
- Punctuation: periods only, bullet points for multiple items',

    behavioral_profile = '- Decision disposition: decides — makes clear decisions, gives definitive answers
- Response completeness: addresses_all — responds to every point raised
- Commitment pattern: specific_next_step — commits to concrete actions with detail ("I will send the revised contract by Thursday")
- Scope behavior: expands_scope — proactively raises related issues or next steps the sender has not mentioned
- IF someone asks for approval → THEN grant or deny with reasoning
- IF someone presents options → THEN pick one decisively and explain why
- IF a problem is raised → THEN propose a solution and assign next steps
- IF a deadline is mentioned → THEN confirm or counter-propose with a specific date',

    preference_profile = '{
  "investment_orientation": {
    "category": "invest_heavy",
    "description": "This user invests by default across all decision types, including low-stakes items where most people would accept good-enough. They shop alternatives rather than accepting renewals, close gaps proactively, fix problems fully rather than patching, investigate root causes, and act preemptively. Their reasoning is action-oriented — when they see a gap between current state and better state, they move to close it without waiting for the problem to force their hand.",
    "confidence": "high",
    "supporting_decisions": 22
  },
  "positional_stance": {
    "category": "yield_light",
    "description": "This user accommodates by default. They follow expert guidance without extensive independent evaluation, concede without demanding reciprocity, and prefer collaborative resolution over confrontation. On high-stakes matters where the cost of yielding is clear and obvious, they will hold ground — but their default is to go along with the recommended path.",
    "confidence": "high",
    "supporting_decisions": 12
  }
}',

    preference_profiled_at = NOW()

WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/


-- ============================================================
-- STEP 2: INSERT NEW EMAIL (worker picks up automatically)
-- ============================================================

INSERT INTO emails (
    user_id,
    email_ref,
    subject,
    sender,
    sender_name,
    sender_email,
    body,
    to_field,
    folder,
    importance,
    has_attachments,
    attachment_names,
    cc_field,
    conversation_id,
    conversation_topic,
    flag_status,
    is_read,
    recipients,
    received_time,
    status
) VALUES (
    'f0fe5970-dbe7-4ed2-b263-6431ba590111',
    'test-pref-' || gen_random_uuid(),
    'Q1 Tax Filing - Discrepancy in Revenue Recognition',
    'Bobby Axelrod <bobby.axelrod5522@gmail.com>',
    'Bobby Axelrod',
    'bobby.axelrod5522@gmail.com',
    E'Nate,\n\nWhile preparing your Q1 estimated tax filing, I found a discrepancy in revenue recognition. You have $142,000 in invoices marked as revenue in Q1, but $38,000 of that appears to be for services not yet delivered (contracts signed but work starts in Q2).\n\nUnder accrual accounting, we should probably defer that $38,000 to Q2, which would reduce your Q1 estimated tax payment by roughly $9,500. However, if your cash flow situation favors paying more now to avoid a larger Q2 hit, we could keep it as-is.\n\nI need you to confirm: (1) whether those contracts have any deliverables completed in Q1 that would justify partial recognition, (2) your preference on timing of the tax payment, and (3) whether you want me to adjust the books now or wait until we have the full Q2 picture.\n\nFiling deadline for the estimate is April 15th, so I need a decision by April 10th.\n\nBobby Axelrod, CPA\nAxelrod Advisory Services',
    'nate.mcbride23@outlook.com',
    'Inbox',
    'Normal',
    false,
    ARRAY[]::text[],
    NULL,
    NULL,
    NULL,
    'NotFlagged',
    true,
    '[]',
    NOW(),
    'unprocessed'
);


-- ============================================================
-- VERIFY: Confirm profile state
-- ============================================================

SELECT
    writing_style_guide IS NOT NULL AS has_style,
    behavioral_profile IS NOT NULL AS has_behavior,
    preference_profile IS NOT NULL AS has_preference,
    preference_profile->'investment_orientation'->>'category' AS investment_cat,
    preference_profile->'positional_stance'->>'category' AS positional_cat,
    preference_profiled_at
FROM profiles
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';


-- ============================================================
-- VERIFY: Pull the generated draft (run after worker processes)
-- ============================================================

SELECT
    d.draft_body,
    d.created_at,
    d.status,
    p.preference_profile->'investment_orientation'->>'category' AS investment,
    p.preference_profile->'positional_stance'->>'category' AS positional
FROM drafts d
JOIN profiles p ON p.id = d.user_id
WHERE d.user_id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111'
ORDER BY d.created_at DESC
LIMIT 1;


-- ============================================================
-- CLEANUP: Reset preference profile after testing
-- ============================================================

/*
UPDATE profiles
SET preference_profile = NULL,
    preference_profiled_at = NULL
WHERE id = 'f0fe5970-dbe7-4ed2-b263-6431ba590111';
*/
