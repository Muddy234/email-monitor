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
Focus on the NEWEST message only. Ignore quoted/forwarded text below signature lines or ">" markers.

Signals:
mc (bool): Financial/legal/deal consequence. Triggers: funding, closing, default, penalty, expiration, compliance, rate lock, guaranty, wire, lien, amendment, maturity.
ar (bool): Sender needs recipient to do something. Includes implicit asks and follow-ups.
ub (bool): Someone downstream is blocked waiting on recipient. Process paused pending their input.
dl (bool): Time constraint on a request. Explicit date/time or urgency language tied to an ask. Ignore dates as context only.
rt (enum none|ack|ans|act|dec): Expected response type. If multiple apply, pick highest: dec>act>ans>ack>none. If rt=none, draft should almost always be false.

Action target (required when ar=true):
target (enum user|other|all|unclear): Who the action/request is directed at.
- user: Action is directed at USER (named, addressed, or sole TO recipient). → draft=true when response needed.
- other: Action is directed at someone else (another name mentioned, USER is CC-only, or body addresses a specific person who is not USER). → draft=false.
- all: Action applies to all recipients equally. → draft=true only if USER must respond individually.
- unclear: Cannot determine who the action targets. → default draft=true to avoid missing real requests.
When ar=false, set target="user" (default, ignored).

Key heuristics for target:
- If body addresses someone by name ("Wes please...", "John, can you...") and that name is NOT USER → target=other.
- If USER is in CC (not TO) and the action doesn't reference USER → target=other.
- If USER is sole TO recipient → target=user.
- If body uses "all" / "everyone" / "team" language → target=all.

Context fields:
- tier: Sender importance. C=critical → bias pri toward high. I=internal (colleagues) → normal. P=professional → normal. U=unknown → bias pri toward low.
- contact_type: Sender's relationship type (internal_colleague, external_legal, external_lender, external_title_escrow, external_contractor, external_investor, external_vendor, external_government, personal, unknown). Use to inform priority and draft decisions.
- significance: Relationship importance (critical, high, medium, low). critical/high senders → bias toward draft=true and higher priority.
- thread_depth: Number of messages in this thread (1=new email). Higher depth in active threads may reduce urgency if others are already engaged.
- unanswered: Whether USER has a prior message in this thread that went unanswered by the sender. true suggests a follow-up the user was waiting for → bias draft=true.

Decisions:
pri (enum high|med|low): Overall priority considering all signals, sender tier, and context.
draft (bool): Whether USER should draft a response. True ONLY when the email requires a response FROM USER specifically. False when: FYI, action targets someone else (target=other), terminal acknowledgments ("Thanks", "Got it"), automated notifications, newsletters, calendar invites, no-response-needed.
reason (string): Under 30 words. When draft=true, write a brief for what the reply should address. When draft=false, explain why no response is needed.

Context format:
Line 1 — Sender: sender_name sender_email|tier|contact_type|significance|thread_depth|unanswered
Line 2 — User: USER user_name|user_email|position (TO or CC)
Line 3 — Recipients: TO: addr1;addr2 | CC: addr1;addr2

Example 1 (action for user — direct request with deadline):
Input: Jane Smith jane@lender.com|C|external_lender|high|1|false
USER: Bob Jones|bjones@company.com|TO
TO: bjones@company.com | CC: (none)
S:Draw Request #4 — Please Review
Bob, please review the attached draw request and approve by Friday.
Output: {"mc":true,"ar":true,"ub":false,"dl":true,"rt":"act","target":"user","pri":"high","draft":true,"reason":"Lender requesting Bob to review and approve draw request by Friday."}

Example 2 (action for someone else — USER is CC):
Input: Gina Kufrovich gina@corridortitle.com|P|external_title_escrow|medium|1|false
USER: Nate McBride|nmcbride@arete-collective.com|CC
TO: wdagestad@polsinelli.com;tmills@arete-collective.com | CC: nmcbride@arete-collective.com
S:CPL — 123 Main St
Wes please see attached CPL.
Output: {"mc":false,"ar":true,"ub":false,"dl":false,"rt":"act","target":"other","pri":"low","draft":false,"reason":"Action directed at Wes (TO recipient), not USER who is CC-only. No response needed."}

Example 3 (FYI / informational — no action needed):
Input: Sarah Miller sarah@arete-collective.com|I|internal_colleague|medium|3|false
USER: Nate McBride|nmcbride@arete-collective.com|CC
TO: tmills@arete-collective.com | CC: nmcbride@arete-collective.com;jcrigler@arete-collective.com
S:Re: Turtle Bay Budget Update
Updated budget spreadsheet attached. Let me know if you have questions.
Output: {"mc":false,"ar":false,"ub":false,"dl":false,"rt":"none","target":"user","pri":"low","draft":false,"reason":"Internal FYI update sent to another recipient. USER is CC for visibility only."}

Example 4 (direct question to user — needs answer):
Input: Dave Wittwer dave@lender.com|C|external_lender|critical|2|true
USER: Nate McBride|nmcbride@arete-collective.com|TO
TO: nmcbride@arete-collective.com | CC: tmills@arete-collective.com
S:Re: Thomas Ranch Phase 2 — Rate Lock
Nate, can you confirm whether you want to lock the rate at 6.25% or float until next week?
Output: {"mc":true,"ar":true,"ub":true,"dl":false,"rt":"dec","target":"user","pri":"high","draft":true,"reason":"Lender asking Nate to decide on rate lock vs float. Confirm preference and timeline."}

Example 5 (terminal acknowledgment — no response needed):
Input: Jim Crigler jim@contractor.com|P|external_contractor|medium|4|false
USER: Nate McBride|nmcbride@arete-collective.com|TO
TO: nmcbride@arete-collective.com | CC: (none)
S:Re: Inspection Schedule
Thanks Nate, got it. We'll be there Monday at 9am.
Output: {"mc":false,"ar":false,"ub":false,"dl":false,"rt":"none","target":"user","pri":"low","draft":false,"reason":"Terminal acknowledgment confirming receipt. No response needed."}\
"""


def build_signal_prompt(email_body, subject, sender_name, sender_email,
                        sender_tier, thread_depth, has_unanswered,
                        user_name="", user_email="", user_position="UNKNOWN",
                        to_field="", cc_field="",
                        contact_type="", significance=""):
    """Build the user message for the signal extraction call."""
    sender_line = (
        f"{sender_name} {sender_email}"
        f"|{sender_tier}"
        f"|{contact_type or 'unknown'}"
        f"|{significance or 'medium'}"
        f"|{thread_depth}"
        f"|{str(has_unanswered).lower()}"
    )
    user_line = f"USER: {user_name}|{user_email}|{user_position}"
    recip_line = f"TO: {to_field or '(none)'} | CC: {cc_field or '(none)'}"
    return f"{sender_line}\n{user_line}\n{recip_line}\nS:{subject}\n\n{email_body}"


def extract_signals(email_body, subject, sender_name, sender_email,
                    sender_tier, thread_depth, has_unanswered,
                    user_name="", user_email="", user_position="UNKNOWN",
                    to_field="", cc_field="",
                    contact_type="", significance="",
                    api_key=None):
    """Call Haiku to extract signals and decisions for a single email.

    Returns:
        tuple: (signal_dict, usage_dict) where signal_dict has keys:
            mc, ar, ub, dl, rt, target, pri, draft, reason
    """
    user_message = build_signal_prompt(
        email_body, subject, sender_name, sender_email,
        sender_tier, thread_depth, has_unanswered,
        user_name=user_name, user_email=user_email,
        user_position=user_position, to_field=to_field, cc_field=cc_field,
        contact_type=contact_type, significance=significance,
    )

    try:
        raw, usage = call_claude(
            prompt=user_message,
            system_prompt=SIGNAL_EXTRACTION_SYSTEM_PROMPT,
            model=resolve_model("haiku"),
            max_tokens=200,
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
                                 has_unanswered, custom_id,
                                 user_name="", user_email="",
                                 user_position="UNKNOWN",
                                 to_field="", cc_field="",
                                 contact_type="", significance=""):
    """Build a Batches API request dict for signal extraction."""
    user_message = build_signal_prompt(
        email_body, subject, sender_name, sender_email,
        sender_tier, thread_depth, has_unanswered,
        user_name=user_name, user_email=user_email,
        user_position=user_position, to_field=to_field, cc_field=cc_field,
        contact_type=contact_type, significance=significance,
    )

    return {
        "custom_id": custom_id,
        "params": {
            "model": resolve_model("haiku"),
            "max_tokens": 200,
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
    ar = _coerce_bool(data.get("ar"))
    target = _coerce_target(data.get("target"))
    draft = _coerce_bool(data.get("draft"))

    # Safety net: if action targets someone else, override draft to False
    if ar and target == "other":
        draft = False

    return {
        "mc": _coerce_bool(data.get("mc")),
        "ar": ar,
        "ub": _coerce_bool(data.get("ub")),
        "dl": _coerce_bool(data.get("dl")),
        "rt": _coerce_rt(data.get("rt")),
        "target": target,
        "pri": _coerce_pri(data.get("pri")),
        "draft": draft,
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
        "target": "user",
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


def _coerce_target(value):
    """Coerce action target enum. Invalid → 'user'."""
    valid = {"user", "other", "all", "unclear"}
    if isinstance(value, str) and value.lower() in valid:
        return value.lower()
    return "user"


def _coerce_reason(value):
    """Coerce reason string. Missing → '', truncate to 200 chars."""
    if not value:
        return ""
    s = str(value)
    return s[:200]
