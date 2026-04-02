"""Prompt constants and helpers for the email pipeline."""


# Draft prompt is now built entirely in drafts.py._build_draft_prompt()
# with structured sections (PERSONALITY PROFILE → NEVER → EMAIL → THREAD SUMMARY
# → CONTACT SUMMARY → "Draft a reply."). No system prompt is used.
DEFAULT_DRAFT_PROMPT_TEMPLATE = None


NOTABLE_SUMMARY_SYSTEM_PROMPT = """\
You are an executive assistant analyzing an email that does not require a direct response \
but is notable enough to warrant a brief summary for the recipient.

Analyze the email and provide a concise summary. Reason through:
1. Situation — What is happening? What is the broader context of this exchange?
2. Sender's intent — What does the sender actually need or want?
3. Key information — What relevant facts, details, or constraints are established?
4. Why no response is needed — Briefly explain why this is FYI / no action required from the recipient.

Output 3-5 sentences of plain text analysis. No JSON, no XML tags, no headers."""


NOTABLE_SUMMARY_USER_TEMPLATE = """\
FROM: {sender_name} <{sender_email}>
SUBJECT: {subject}

EMAIL BODY:
{body}"""


def build_notable_summary_prompt(email_data, conversation_history=None):
    """Build the user message for a notable email summary call."""
    from .pre_process import isolate_new_content, strip_reply_markers, truncate_smart, _is_forward_subject

    subject = email_data.get("subject", "")
    raw_body = email_data.get("body", "") or ""
    if conversation_history:
        prior_bodies = [m.get("body") or "" for m in conversation_history if m.get("body")]
        body = isolate_new_content(raw_body, prior_bodies, subject=subject)
    else:
        body = strip_reply_markers(raw_body) if not _is_forward_subject(subject) else raw_body
    body = truncate_smart(body, max_tokens=1000)

    prompt = NOTABLE_SUMMARY_USER_TEMPLATE.format(
        sender_name=email_data.get("sender_name", "Unknown"),
        sender_email=email_data.get("sender", ""),
        subject=email_data.get("subject", "(no subject)"),
        body=body,
    )

    # Append thread context if available
    if conversation_history:
        sorted_msgs = sorted(
            conversation_history, key=lambda m: m.get("received_time") or ""
        )
        thread_parts = ["\n\nTHREAD CONTEXT (prior messages in this conversation):"]
        for msg in sorted_msgs[:5]:  # cap at 5 messages
            sender = msg.get("sender_name") or msg.get("sender_email", "Unknown")
            date = msg.get("received_time", "")
            msg_body = (msg.get("body") or "")[:500]
            if msg_body:
                thread_parts.append(f"--- {sender} ({date}) ---")
                thread_parts.append(msg_body)
        if len(thread_parts) > 1:
            prompt += "\n".join(thread_parts)

    return prompt


def get_draft_prompt_template():
    """Return the default draft prompt template."""
    return DEFAULT_DRAFT_PROMPT_TEMPLATE
