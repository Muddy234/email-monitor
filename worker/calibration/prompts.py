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

CORRECTION_GENERATION_PROMPT = """\
Here is the user's personality profile that was used to generate the draft.

{personality_profile}

Here is what the user actually wrote:
{actual_reply}

Here is what the draft produced:
{generated_draft}

The draft missed on these dimensions: {failing_dimensions}

What specific instruction is missing from the personality profile
that would have caused the draft to match the user's actual reply?
Write the instruction exactly as it should appear in the prompt.
One instruction per failure. No preamble."""
