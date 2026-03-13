"""Haiku signal extraction — single LLM call per email for classification signals + decisions.

Replaces both the old regex-based scorer.py and the Haiku classify call.
Returns 5 explanatory signals (mc, ar, ub, dl, rt) plus 3 decision fields
(pri, draft, reason) that drive the pipeline directly.
"""

import json
import logging

from .api_client import call_claude, resolve_model

logger = logging.getLogger("worker")

SIGNAL_EXTRACTION_SYSTEM_PROMPT = """\
Classify this email. Return JSON only, no other text.

Signals:
mc (bool): Financial/legal/deal consequence. Triggers: funding, closing, default, penalty, expiration, compliance, rate lock, guaranty, wire, lien, amendment, maturity.
ar (bool): Sender needs recipient to do something. Includes implicit asks and follow-ups.
ub (bool): Someone downstream is blocked waiting on recipient. Process paused pending their input.
dl (bool): Time constraint on a request. Explicit date/time or urgency language tied to an ask. Ignore dates as context only.
rt (enum none|ack|ans|act|dec): Expected response type. If multiple apply, pick highest: dec>act>ans>ack>none.

Decisions:
pri (enum high|med|low): Overall priority considering all signals, sender tier, and context.
draft (bool): Whether this email warrants a draft response. True for questions, actions, decisions. False for FYI, acknowledgments, no-response-needed.
reason (string): Why draft is true or false. Under 30 words. Also serves as the draft brief for response generation.

Context line format: sender|tier|thread_depth|unanswered
Tiers: C=critical, I=internal, P=professional, U=unknown

Example:
Input: Jane Smith jane@lender.com|C|1|false
S:Draw Request #4 — Approved
Your draw request has been approved and funds will wire Thursday. No action needed on your end.
Output: {"mc":true,"ar":false,"ub":false,"dl":false,"rt":"none","pri":"med","draft":false,"reason":"Informational notice of approved draw request. No action or response needed from recipient."}\
"""


def build_signal_prompt(email_body, subject, sender_name, sender_email,
                        sender_tier, thread_depth, has_unanswered):
    """Build the user message for the signal extraction call.

    Args:
        email_body: Pre-processed email body (stripped, truncated).
        subject: Email subject line.
        sender_name: Display name of sender.
        sender_email: Sender email address.
        sender_tier: One of 'C', 'I', 'P', 'U'.
        thread_depth: Number of messages in thread.
        has_unanswered: Whether there's a prior unanswered message from this sender.

    Returns:
        str: The formatted user message.
    """
    sender_line = (
        f"{sender_name} {sender_email}"
        f"|{sender_tier}"
        f"|{thread_depth}"
        f"|{str(has_unanswered).lower()}"
    )
    return f"{sender_line}\nS:{subject}\n\n{email_body}"


def extract_signals(email_body, subject, sender_name, sender_email,
                    sender_tier, thread_depth, has_unanswered,
                    api_key=None):
    """Call Haiku to extract signals and decisions for a single email.

    Args:
        email_body: Pre-processed email body.
        subject: Email subject line.
        sender_name: Sender display name.
        sender_email: Sender email address.
        sender_tier: One of 'C', 'I', 'P', 'U'.
        thread_depth: Thread message count.
        has_unanswered: Prior unanswered from this sender.
        api_key: Anthropic API key (None → env var).

    Returns:
        tuple: (signal_dict, usage_dict) where signal_dict has keys:
            mc, ar, ub, dl, rt, pri, draft, reason
    """
    user_message = build_signal_prompt(
        email_body, subject, sender_name, sender_email,
        sender_tier, thread_depth, has_unanswered,
    )

    try:
        raw, usage = call_claude(
            prompt=user_message,
            system_prompt=SIGNAL_EXTRACTION_SYSTEM_PROMPT,
            model=resolve_model("haiku"),
            max_tokens=150,
            timeout=30,
            api_key=api_key,
            temperature=0,
            cache_system_prompt=True,
        )
    except Exception as e:
        logger.warning(f"Signal extraction API error: {e}")
        return _fallback_signals("Signal extraction failed"), {}

    return parse_signal_response(raw), usage


def extract_signals_batch_params(email_body, subject, sender_name,
                                 sender_email, sender_tier, thread_depth,
                                 has_unanswered, custom_id):
    """Build a Batches API request dict for signal extraction.

    Returns:
        dict: With 'custom_id' and 'params' keys for the Batches API.
    """
    user_message = build_signal_prompt(
        email_body, subject, sender_name, sender_email,
        sender_tier, thread_depth, has_unanswered,
    )

    return {
        "custom_id": custom_id,
        "params": {
            "model": resolve_model("haiku"),
            "max_tokens": 150,
            "temperature": 0,
            "system": [
                {
                    "type": "text",
                    "text": SIGNAL_EXTRACTION_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_message}],
        },
    }


def parse_signal_response(raw_text):
    """Parse and coerce Haiku's JSON response into a clean signal dict.

    Handles three failure modes:
    1. None/empty → full fallback
    2. Malformed JSON → full fallback
    3. Valid JSON with wrong types/missing keys → per-field coercion

    Returns:
        dict: Coerced signal dict.
    """
    if not raw_text:
        return _fallback_signals("Empty response")

    # Strip any markdown fencing
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"Signal extraction JSON parse failed: {raw_text[:200]}")
        return _fallback_signals("JSON parse failed")

    if not isinstance(data, dict):
        return _fallback_signals("Response is not a JSON object")

    return _coerce_signals(data)


def _coerce_signals(data):
    """Per-field coercion: partial results are better than no results."""
    return {
        "mc": _coerce_bool(data.get("mc")),
        "ar": _coerce_bool(data.get("ar")),
        "ub": _coerce_bool(data.get("ub")),
        "dl": _coerce_bool(data.get("dl")),
        "rt": _coerce_rt(data.get("rt")),
        "pri": _coerce_pri(data.get("pri")),
        "draft": _coerce_bool(data.get("draft")),
        "reason": _coerce_reason(data.get("reason")),
    }


def _fallback_signals(reason_msg):
    """Full fallback when extraction fails entirely."""
    return {
        "mc": False,
        "ar": False,
        "ub": False,
        "dl": False,
        "rt": "none",
        "pri": "low",
        "draft": False,
        "reason": reason_msg,
    }


def _coerce_bool(value):
    """Coerce to bool. Truthy strings → True, missing → False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _coerce_rt(value):
    """Coerce response type enum. Invalid → 'none'."""
    valid = {"none", "ack", "ans", "act", "dec"}
    if isinstance(value, str) and value.lower() in valid:
        return value.lower()
    return "none"


def _coerce_pri(value):
    """Coerce priority enum. Invalid → 'low'."""
    valid = {"high", "med", "low"}
    if isinstance(value, str) and value.lower() in valid:
        return value.lower()
    return "low"


def _coerce_reason(value):
    """Coerce reason string. Missing → '', truncate to 200 chars."""
    if not value:
        return ""
    s = str(value)
    return s[:200]
