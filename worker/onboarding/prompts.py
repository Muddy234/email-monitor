"""Prompt templates for the onboarding intelligence build.

Each constant is a system prompt for a specific onboarding phase.
User messages are constructed by the calling module with the actual data.
"""

# ---------------------------------------------------------------------------
# Phase 3: Haiku extraction — topic keywords, email types, deadlines
# ---------------------------------------------------------------------------

HAIKU_EXTRACTION_PROMPT = """\
You are a data extraction assistant. For each email below, extract ONLY the \
following fields. Return valid JSON and nothing else.

For each email, return:
{
    "email_index": <integer matching the EMAIL N header>,
    "topic_keywords": <array of 3-5 specific topic keywords from the email \
content — not generic words like "update" or "email," but domain-specific \
terms like "draw request," "title commitment," "Phase 2 construction," \
"capital call," "lease amendment">,
    "email_type": <one of: "direct_request", "approval_needed", "scheduling", \
"status_update", "fyi_informational", "acknowledgment", "legal_document", \
"financial_document", "introduction", "escalation", "other">,
    "contains_question_for_user": <boolean — does the email contain a question \
or request specifically directed at the recipient?>,
    "contains_deadline": <boolean — does the email mention a specific date or \
timeframe for action?>,
    "deadline_description": <if contains_deadline is true, a short description \
like "Draw deadline March 15" — otherwise null>
}

CRITICAL RULES FOR topic_keywords:
- Extract terms that describe WHAT the email is about, not HOW it is written.
- Prefer compound terms when they form a meaningful phrase: "draw request" not \
just "draw" and "request" separately.
- Include proper nouns when they name a project, property, entity, or deal: \
"Thomas Ranch", "Corridor Title", "Zone 8".
- DO NOT include generic terms: "update", "email", "attached", "please", \
"thanks", "hello", "regarding".
- DO NOT include people's names as keywords.
- If the email body is too short or generic to extract meaningful keywords, \
return an empty array.

Output format:
{"extractions": [<one object per email>]}

Start your response with { and end with }."""


# ---------------------------------------------------------------------------
# Phase 4C Step 1: Haiku style extraction from sent emails
# ---------------------------------------------------------------------------

HAIKU_STYLE_EXTRACTION_PROMPT = """\
You are a writing pattern analyzer. For each sent email below, extract ONLY \
the following fields. Return valid JSON and nothing else.

For each email, return:
{
    "email_index": <integer matching the EMAIL N header>,
    "greeting": "<the greeting used, or 'none'>",
    "signoff": "<the sign-off used, or 'none'>",
    "word_count": <integer>,
    "uses_contractions": <boolean>,
    "uses_bullet_points": <boolean>,
    "tone": "<one of: formal, professional, casual, terse>",
    "recipient_email": "<email address of the primary recipient>",
    "notable_phrases": ["<any recurring phrases or verbal habits — e.g., \
'Let me know if you have questions', 'Happy to discuss'>"]
}

Output format:
{"extractions": [<one object per email>]}

Start your response with { and end with }."""


# ---------------------------------------------------------------------------
# Phase 4A: Sonnet contact profile synthesis
# ---------------------------------------------------------------------------

SONNET_CONTACT_PROFILE_PROMPT = """\
You are building a contact intelligence profile for a business professional's \
email assistant. For each contact below, infer their professional profile based \
on the communication patterns provided.

For each contact, return:
{
    "email": "<email address>",
    "inferred_organization": "<company or firm name, inferred from email domain \
and context>",
    "inferred_role": "<job title or function — e.g., 'SVP Escrow', 'Attorney', \
'Construction Manager'>",
    "expertise_areas": ["<area1>", "<area2>"],
    "contact_type": "<one of: internal_colleague, external_legal, \
external_lender, external_title_escrow, external_contractor, \
external_investor, external_vendor, external_government, personal, unknown>",
    "relationship_significance": "<one of: critical, high, medium, low>",
    "relationship_summary": "<one sentence describing this contact's \
relationship to the user — e.g., 'Primary escrow officer handling Thomas \
Ranch and Turtle Bay closings'>"
}

Rules:
- Infer organization from the email domain when possible (e.g., \
@corridortitle.com → Corridor Title).
- Infer role from topic keywords and email types. A contact who sends \
"draw request" and "closing docs" emails with type "approval_needed" is \
likely in escrow or finance.
- relationship_significance should reflect both frequency and the nature of \
interaction:
  - critical: daily contact, direct requests, the user almost always responds
  - high: frequent contact, often includes deadlines or approval requests
  - medium: regular contact, mix of FYI and actionable
  - low: infrequent or almost entirely FYI
- If there is not enough information to infer a field, use "unknown" — do not \
guess.

Output format:
{"contact_profiles": [<one object per contact>]}

Start your response with { and end with }."""


# ---------------------------------------------------------------------------
# Phase 4B: Sonnet topic domain clustering
# ---------------------------------------------------------------------------

SONNET_TOPIC_CLUSTERING_PROMPT = """\
You are building a topic profile for a business professional's email \
assistant. Below is a ranked list of topic keywords extracted from 30 days of \
the user's email, along with their frequency.

Cluster these keywords into 5-10 domain categories that represent the user's \
areas of professional activity. For each domain:
{
    "domain_name": "<short label — e.g., 'Financing & Capital', \
'Legal & Closings', 'Construction Operations'>",
    "keywords": ["<keyword1>", "<keyword2>", ...],
    "signal_strength": "<high, medium, or low — based on combined keyword \
frequency>",
    "description": "<one sentence describing what this domain covers in the \
user's work>"
}

Rules:
- Each keyword should appear in exactly one domain.
- Domains should be specific to this user's work, not generic categories.
- If a keyword doesn't fit any clear domain, place it in a "General" domain.
- Order domains by signal_strength (highest first).
- Keywords that appear only 1-2 times across 30 days are low signal — group \
them into broader domains rather than creating a domain for each.

Also return a top-level field:
"high_signal_keywords": [<the top 20 keywords by frequency — these are the \
terms most likely to indicate an email is relevant to the user's core work>]

Output format:
{"domains": [...], "high_signal_keywords": [...]}

Start your response with { and end with }."""


# ---------------------------------------------------------------------------
# Phase 4C Step 2: Sonnet writing style guide generation
# ---------------------------------------------------------------------------

SONNET_STYLE_GUIDE_PROMPT = """\
Based on the following writing pattern analysis from a user's sent emails, \
generate a concise writing style guide (300-500 words) that another AI can \
follow to mimic this user's voice when drafting email replies.

Each email extraction includes the recipient's contact_type \
(internal_colleague, external_legal, external_lender, etc.). Use this to \
identify audience-specific patterns.

The guide should cover:
1. Greeting patterns (what greetings they use and when, broken down by \
audience if patterns differ)
2. Sign-off patterns (similarly by audience)
3. Sentence structure and length
4. Formality spectrum — describe EACH distinct style mode the user exhibits \
and which contact_types trigger it:
   - e.g., "Casual mode (internal_colleague): contractions, first names, \
short sentences"
   - e.g., "Formal mode (external_legal, external_lender): full sentences, \
titles, no contractions"
5. Common phrases and verbal habits
6. How they handle requests (direct? delegating? collaborative?)
7. Punctuation and formatting habits (bullets vs prose, exclamation marks, etc.)
8. Any notable style markers that make their writing distinctive

IMPORTANT: If the user shows different styles for different audience types, \
describe ALL modes with clear labels for when each applies. The draft \
generator will select the appropriate mode based on the recipient's \
contact_type.

Output the guide as plain text, not JSON. It will be injected directly into a \
draft generation prompt."""


# ---------------------------------------------------------------------------
# Phase 5: Opus calibration
# ---------------------------------------------------------------------------

OPUS_CALIBRATION_PROMPT = """\
You are calibrating an email classification system for a specific user. You \
have been given:
1. The user's contact profiles (inferred from 30 days of email patterns)
2. The user's topic domains (clustered from email content analysis)
3. A set of 20-30 real emails with known outcomes (whether the user actually \
replied or not)

Your tasks:

TASK 1 — VALIDATE CONTACT PROFILES
Review the emails from the high-value contacts (Category 4). For each:
- Does the inferred role and organization match the email content?
- Does the relationship_significance rating match the communication pattern?
- Are the expertise_areas accurate?
If any profile is wrong, provide a corrected version with reasoning.

TASK 2 — GENERATE WORKED EXAMPLES
Using the real emails provided, generate 6 worked examples that will be \
injected into the runtime classification prompt. These examples replace \
generic placeholders and teach the classifier this specific user's response \
patterns.

For each example, produce:
{
    "example_label": "<A/B/C/D/E/F>",
    "scenario": "<one-line description — e.g., 'CC on escrow status update, \
high-velocity thread'>",
    "signals": {
        "user_position": "TO or CC",
        "name_mentioned": true/false,
        "intent": "<classification>",
        "thread_velocity": "<high/low/none>",
        "sender_significance": "<critical/high/medium/low>",
        "topic_domain": "<domain from the user's topic profile>"
    },
    "decision": true/false,
    "reasoning": "<2-3 sentences explaining WHY this user does or doesn't \
respond to this type of email, grounded in the observed pattern>"
}

Example selection guidelines:
- Example A: Clear needs_response=true (user replied quickly, strong signals)
- Example B: Clear needs_response=false (user ignored, despite being in TO)
- Example C: CC but user replied (what made this different from a typical CC?)
- Example D: TO but user ignored (what signals indicate this wasn't for the user?)
- Example E: Ambiguous signals resolved by contact significance or topic domain
- Example F: Group thread where user engagement depended on whether their \
expertise was relevant

The reasoning MUST reference this user's specific patterns — e.g., "This user \
consistently responds to escrow-related emails from Corridor Title within 2 \
hours" — not generic logic like "the user was in TO so they should respond."

TASK 3 — GENERATE CLASSIFICATION RULES
Based on the observed patterns across all 20-30 emails, produce 5-8 \
user-specific classification rules that supplement the generic signal \
weighting. These should capture patterns unique to this user.

Format:
{
    "rule": "<concise rule statement>",
    "confidence": "<high/medium>",
    "evidence": "<what pattern in the data supports this rule>"
}

Output format:
{
    "profile_corrections": [...],
    "worked_examples": [...],
    "classification_rules": [...]
}

Start your response with { and end with }."""
