"""Prompt templates for the calibration layer."""

BEHAVIORAL_SCORING_PROMPT = """\
You are scoring an email reply against behavioral dimensions.
The user's actual sent reply is provided below.

Classify the reply on each dimension. Output exactly three lines:
DECISIVENESS: {{decides | proposes_solution | defers | delegates | no_signal}}
THOROUGHNESS: {{addresses_all | key_point_only | no_signal}}
SPECIFICITY: {{specific_next_step | conditional_decision | vague_forward | no_signal}}

No explanation. No other text.

ORIGINAL EMAIL (what the user was replying to):
{incoming_email_body}

USER'S REPLY:
{sent_email_body}"""

PREFERENCE_SCORING_PROMPT = """\
You are scoring an email reply against preference dimensions.

Classify the reply on each dimension. Output exactly two lines:
INVESTMENT: {{active | selective | conservative | no_signal}}
POSITIONAL: {{advancing | measured | yielding | no_signal}}

No explanation. No other text.

ORIGINAL EMAIL (what the user was replying to):
{incoming_email_body}

THREAD CONTEXT (if any):
{thread_summary_or_none}

USER'S REPLY:
{sent_email_body}"""

CONTEXTUAL_SCORING_PROMPT = """\
You are evaluating a generated email draft against the email the user
actually sent. Score the draft on four dimensions.

ORIGINAL INCOMING EMAIL:
{incoming_email}

THREAD CONTEXT:
{thread_summary}

USER'S ACTUAL SENT REPLY:
{actual_reply}

GENERATED DRAFT:
{generated_draft}

Score each dimension. Output exactly four lines:
CONTENT_ALIGNMENT: {{match | partial | hard_miss}} — Did the draft make the same substantive decision as the user?
FABRICATION: {{none | detected}} — Did the draft commit to, reference, or state anything not present in the incoming email or thread? This includes fabricated action items, deadlines, attachment references, and self-generated commitments.
COMPREHENSION: {{pass | fail}} — Did the draft correctly understand what was being asked or discussed?
ATTRIBUTION: {{pass | fail}} — Did the draft respond to the right person about the right thing, and correctly identify whether the user was the appropriate respondent?

No explanation. No other text."""

BEHAVIORAL_COMPARISON_PROMPT = """\
You are comparing a generated draft against the user's actual reply \
on behavioral dimensions.

ORIGINAL EMAIL (what the user was replying to):
{incoming_email_body}

USER'S ACTUAL REPLY:
{actual_reply}

GENERATED DRAFT:
{generated_draft}

For each dimension, does the draft match the user's behavioral approach?
Output exactly three lines:
DECISIVENESS: {{match | adjacent | hard_miss}}
THOROUGHNESS: {{match | adjacent | hard_miss}}
SPECIFICITY: {{match | adjacent | hard_miss}}

"match" = same approach. "adjacent" = similar but not identical. \
"hard_miss" = fundamentally different approach (e.g., user decided \
but draft deferred, or user addressed all points but draft only \
addressed one).

No explanation. No other text."""

PREFERENCE_COMPARISON_PROMPT = """\
You are comparing a generated draft against the user's actual reply \
on preference dimensions.

ORIGINAL EMAIL (what the user was replying to):
{incoming_email_body}

THREAD CONTEXT (if any):
{thread_summary_or_none}

USER'S ACTUAL REPLY:
{actual_reply}

GENERATED DRAFT:
{generated_draft}

For each dimension, does the draft match the user's preference approach?
Output exactly two lines:
INVESTMENT: {{match | adjacent | hard_miss | not_applicable}}
POSITIONAL: {{match | adjacent | hard_miss | not_applicable}}

"match" = same level of investment/stance. "adjacent" = similar but \
not identical. "hard_miss" = fundamentally different (e.g., user invested \
heavily but draft was conservative, or user pushed back but draft yielded). \
"not_applicable" = this email does not involve that dimension.

No explanation. No other text."""

CORRECTION_GENERATION_PROMPT = """\
Here is the user's personality profile that was used to generate the draft.

{personality_profile}

Here is what the user actually wrote:
{actual_reply}

Here is what the draft produced:
{generated_draft}

The draft missed on these dimensions: {failing_dimensions}

Identify the GAP between the profile and the user's actual behavior.
Write ONE concise observation (2-3 sentences max) that captures what \
the profile fails to convey about how this user thinks or communicates.
Focus on perspective and reasoning patterns, not surface-level rules \
like "use a greeting" or "be more concise."

Do not repeat anything already in the profile. If the gap is purely \
stylistic (greeting/signoff/formatting), respond with: NO_GAP

Your observation:"""
