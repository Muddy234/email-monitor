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
    "pleasantry_level": "<one of: none, minimal, standard, warm>",
    "notable_phrases": ["<any recurring phrases or verbal habits — e.g., \
'Let me know if you have questions', 'Happy to discuss'>"]
}

Pleasantry level definitions:
- "none": no greeting, no thank you, straight to content
- "minimal": brief greeting or thanks ("Thanks," "Hi Michael,")
- "standard": greeting + closing pleasantry
- "warm": effusive thanks, personal remarks, extra warmth

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
5. Pleasantry calibration — When does the user skip thank-yous and get \
straight to business? When warm? Break down by contact_type if patterns differ. \
   - e.g., "Minimal pleasantries with internal colleagues. Standard warmth \
with external title/escrow contacts. Warm with long-standing external partners."
6. Common phrases and verbal habits
7. How they handle requests (direct? delegating? collaborative?)
8. Punctuation and formatting habits (bullets vs prose, exclamation marks, etc.)
9. Any notable style markers that make their writing distinctive

IMPORTANT: If the user shows different styles for different audience types, \
describe ALL modes with clear labels for when each applies. The draft \
generator will select the appropriate mode based on the recipient's \
contact_type.

Output the guide as plain text, not JSON. It will be injected directly into a \
draft generation prompt."""


# ---------------------------------------------------------------------------
# Phase 4C-1b: Haiku behavioral extraction from sent emails (with parent)
# ---------------------------------------------------------------------------

HAIKU_BEHAVIORAL_EXTRACTION_PROMPT = """\
You are a behavioral pattern analyzer. For each sent email (paired with the \
inbound message it replies to, when available), extract ONLY the following \
fields. Return valid JSON and nothing else.

For paired emails (sent + inbound parent), return:
{
    "email_index": <integer matching the PAIR N or EMAIL N header>,
    "contact_type": "<the contact_type label provided in the header>",
    "decision_type": "<one of: decides, proposes_solution, defers, delegates, \
asks_for_info, diagnoses, n/a>",
    "response_completeness": "<one of: addresses_all, key_point_only, partial>",
    "commitment_pattern": "<one of: specific_next_step, vague_forward, \
redirected_ask, none>",
    "scope_behavior": "<one of: stays_narrow, adds_context, expands_scope>"
}

For unpaired emails (no inbound parent available), return:
{
    "email_index": <integer>,
    "contact_type": "<the contact_type label provided>",
    "decision_type": null,
    "response_completeness": null,
    "commitment_pattern": "<observable from sent email alone>",
    "scope_behavior": "<observable from sent email alone>"
}

Field definitions:
- decision_type: How the user handles the situation presented in the inbound.
  - "decides": user makes a clear decision or gives a definitive answer
  - "proposes_solution": user identifies a problem and offers a specific fix \
or approach (not just deciding yes/no, but constructing a path forward)
  - "defers": user explicitly postpones or says they'll handle it later
  - "delegates": user routes the task to someone else
  - "asks_for_info": user needs more information before acting
  - "diagnoses": user asks targeted diagnostic questions to narrow down a \
problem before committing to a course of action
  - "n/a": the inbound was not requesting a decision (pure FYI, etc.)
- response_completeness: How thoroughly the user addresses the inbound.
  - "addresses_all": user responds to every question or point raised
  - "key_point_only": user zeroes in on the single most important issue and \
skips or defers the rest
  - "partial": user addresses some points but ignores others without apparent \
intent (incomplete rather than selective)
- commitment_pattern: What forward-looking commitment the user makes.
  - "specific_next_step": user commits to a concrete action with detail \
(e.g., "I'll send the revised draw schedule by Thursday")
  - "vague_forward": user references a future action without specifics \
(e.g., "will follow up," "let me look into this")
  - "redirected_ask": user turns the commitment back to the sender or a \
third party (e.g., "can you send me the updated numbers?")
  - "none": no forward commitment — purely reactive or acknowledgment
- scope_behavior: Whether the user stays within the boundaries of the inbound \
or expands the conversation.
  - "stays_narrow": user responds only to what was asked, nothing more
  - "adds_context": user provides relevant information the sender didn't ask \
for but would find useful (e.g., flagging a related issue, adding a caveat)
  - "expands_scope": user broadens the conversation to adjacent topics or \
raises new issues beyond the original thread

Additional context: Some emails include a [CONTEXT: ...] block with pre-computed \
metadata about the interaction:
- response_latency_hours: how quickly the user replied (in hours)
- inbound_has_question: whether the inbound contained a question mark
- inbound_has_action_language: whether the inbound contained action-oriented \
language (e.g., "please approve", "can you")
- subject_type: "new", "reply", "forward", or "chain_forward"
- thread_depth: total messages in the conversation thread

Use these to inform your extraction — for example, a fast response with \
decision_type="decides" and commitment_pattern="specific_next_step" suggests \
the user prioritizes that type of request. Do NOT include these fields in \
your output; they are input-only context.

Output format:
{"extractions": [<one object per email/pair>]}

Start your response with { and end with }."""


# ---------------------------------------------------------------------------
# Phase 4C-3: Sonnet behavioral profile synthesis
# ---------------------------------------------------------------------------

SONNET_BEHAVIORAL_PROFILE_PROMPT = """\
Based on the following behavioral pattern analysis from a user's sent emails \
(paired with the inbound messages they were replying to), generate a behavioral \
profile (300-500 words) that another AI can follow to replicate this user's \
content and decision-making patterns when drafting email replies.

IMPORTANT: The contact_type labels in the extraction data are APPROXIMATE \
(domain-based: same org = internal, different org = external). The authoritative \
contact profiles with specific types (external_lender, external_legal, etc.) are \
provided separately below. Use the authoritative profiles to re-map and refine \
the extraction data. Do not treat extraction-time labels and profile labels as \
different taxonomies.

The profile MUST cover these 4 dimensions:

1. Decision disposition — How the user handles decisions. Values include: \
decides, proposes_solution, defers, delegates, asks_for_info, diagnoses. \
Describe EACH mode observed and the conditions that trigger it. Note when the \
user constructs a solution vs simply approving/rejecting, and when they run \
diagnostics vs immediately requesting info. \
e.g., "Decides immediately for operational requests from internal contacts. \
Proposes solutions for construction issues rather than just approving vendor \
suggestions. Defers to partner for financial/legal decisions. Diagnoses before \
acting on anomalies — asks targeted questions to narrow down the issue."

2. Response completeness — Does the user address every point raised, zero in on \
the key issue, or respond partially? Describe the pattern and what triggers each \
mode. \
e.g., "Addresses all points for external legal/lender contacts. Key-point-only \
for internal operational threads — picks the blocking issue and ignores the rest. \
Partial responses are rare and usually indicate low-priority threads."

3. Commitment patterns — What forward-looking commitments does the user make? \
Does the user commit to specific next steps, give vague forward references, or \
redirect the ask back to the sender? Describe by situation type. \
e.g., "Specific next steps for items in their direct control ('I'll send the \
revised schedule by Thursday'). Redirected asks for items requiring others' input \
('Can you send me the updated numbers?'). Vague forward references are rare — \
the user almost always either commits specifically or redirects."

4. Scope behavior — Does the user stay narrow, add unrequested context, or expand \
the conversation? Describe when each mode appears. \
e.g., "Stays narrow on routine approvals. Adds context when they spot a related \
risk the sender may not be aware of ('heads up — the Phase 2 permit is still \
pending, which could affect this timeline'). Rarely expands scope outright."

CRITICAL INSTRUCTIONS:
- Do NOT produce a single flattened average. Behavior varies by context.
- Identify MODES and label what triggers each mode (contact_type, situation type, \
information completeness).
- If no clear pattern exists for a dimension, state "no consistent pattern \
observed" rather than guessing. The draft generator will fall back to neutral \
behavior for uncertain dimensions.
- Use concrete examples from the data to anchor each pattern.
- If a pattern only appears with certain contact_types, say so explicitly.
- This profile will be injected alongside a separate WRITING STYLE GUIDE. This \
profile governs WHAT the reply contains (decisions, commitments, scope). The \
style guide governs HOW it is written (tone, vocabulary, pleasantries, structure). \
Do not repeat style information here.

Output as plain text, not JSON."""
