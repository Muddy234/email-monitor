"""Phase 4A/4B/4C-2: Sonnet-powered synthesis for onboarding.

Phase 4A: Contact profile synthesis — infer org, role, expertise.
Phase 4B: Topic domain clustering — group keywords into domains.
Phase 4C-2: Writing style guide generation — audience-aware style guide.
"""

import json
import logging
import re

from onboarding.extraction import _parse_json_response
from onboarding.prompts import (
    SONNET_CONTACT_PROFILE_PROMPT,
    SONNET_TOPIC_CLUSTERING_PROMPT,
    SONNET_STYLE_GUIDE_PROMPT,
    SONNET_BEHAVIORAL_PROFILE_PROMPT,
)
from onboarding.retry import call_with_retry

logger = logging.getLogger("worker.onboarding")


def synthesize_contacts(contact_freq, response_rates, extractions):
    """Phase 4A: Build contact profiles via Sonnet.

    Aggregates per-contact data from statistics and Haiku extractions,
    then calls Sonnet to infer professional profiles.

    Args:
        contact_freq: Per-sender frequency dict from Phase 2A.
        response_rates: Per-sender response rate dict from Phase 2B.
        extractions: Haiku extraction results (list of per-email dicts).

    Returns:
        list[dict]: Contact profile dicts, or None on failure.
    """
    # Build per-contact input summaries
    contact_inputs = _build_contact_inputs(contact_freq, response_rates, extractions)

    if not contact_inputs:
        logger.warning("No contacts to synthesize")
        return [], {}

    # Format for Sonnet
    prompt_text = _format_contact_prompt(contact_inputs)

    response, usage = call_with_retry(
        prompt=prompt_text,
        system_prompt=SONNET_CONTACT_PROFILE_PROMPT,
        model="sonnet",
        max_tokens=4096,
        temperature=0,
        cache_system_prompt=True,
    )

    if not response:
        logger.error("Contact synthesis: no response from Sonnet")
        return None, usage

    data = _parse_json_response(response)
    if data is None:
        logger.error(f"Contact synthesis: invalid JSON from Sonnet — {response[:200]}")
        return None, usage
    profiles = data.get("contact_profiles", [])
    logger.info(f"Synthesized {len(profiles)} contact profiles")
    return profiles, usage


def synthesize_topics(keyword_frequencies):
    """Phase 4B: Cluster keywords into topic domains via Sonnet.

    Args:
        keyword_frequencies: dict of keyword -> count.

    Returns:
        dict with 'domains' and 'high_signal_keywords', or None on failure.
    """
    if not keyword_frequencies:
        logger.warning("No keywords to cluster")
        return {"domains": [], "high_signal_keywords": []}, {}

    # Format ranked keyword list
    sorted_kw = sorted(keyword_frequencies.items(), key=lambda x: x[1], reverse=True)
    total_count = len(sorted_kw)
    lines = [f"- \"{kw}\" (frequency: {count})" for kw, count in sorted_kw[:150]]
    header = f"Ranked keywords by frequency (top 150 of {total_count}):\n" if total_count > 150 else "Ranked keywords by frequency:\n"
    prompt_text = header + "\n".join(lines)

    response, usage = call_with_retry(
        prompt=prompt_text,
        system_prompt=SONNET_TOPIC_CLUSTERING_PROMPT,
        model="sonnet",
        max_tokens=4096,
        temperature=0,
        cache_system_prompt=True,
    )

    if not response:
        logger.error("Topic synthesis: no response from Sonnet")
        return None, usage

    data = _parse_json_response(response)
    if data is None:
        logger.error(f"Topic synthesis: invalid JSON from Sonnet — {response[:200]}")
        return None, usage
    domains = data.get("domains", [])
    high_signal = data.get("high_signal_keywords", [])
    logger.info(f"Clustered into {len(domains)} domains, {len(high_signal)} high-signal keywords")
    return {"domains": domains, "high_signal_keywords": high_signal}, usage


def synthesize_style_guide(style_features, contact_profiles):
    """Phase 4C-2: Generate a writing style guide via Sonnet.

    Needs contact_type from Phase 4A to stratify by audience.

    Args:
        style_features: List of per-email style dicts from Haiku (Phase 4C-1).
        contact_profiles: List of contact profile dicts from Phase 4A.

    Returns:
        str: Plain text style guide, or None on failure.
    """
    if not style_features:
        logger.warning("No style features to synthesize")
        return None, {}

    # Build contact_type lookup from profiles
    contact_type_map = {}
    for profile in (contact_profiles or []):
        email = profile.get("email", "").lower()
        if email:
            contact_type_map[email] = profile.get("contact_type", "unknown")

    # Enrich style features with contact_type
    enriched = []
    for feat in style_features:
        recipient = (feat.get("recipient_email") or "").lower()
        feat_copy = dict(feat)
        feat_copy["contact_type"] = contact_type_map.get(recipient, "unknown")
        enriched.append(feat_copy)

    # Format for Sonnet
    prompt_text = (
        f"Writing pattern analysis from {len(enriched)} sent emails:\n\n"
        + json.dumps(enriched)
    )

    response, usage = call_with_retry(
        prompt=prompt_text,
        system_prompt=SONNET_STYLE_GUIDE_PROMPT,
        model="sonnet",
        max_tokens=4096,
        temperature=0.3,
        cache_system_prompt=True,
    )

    if not response:
        logger.error("Style guide synthesis: no response from Sonnet")
        return None, usage

    cleaned = _clean_synthesis_output(response)
    logger.info(f"Generated style guide ({len(cleaned)} chars)")
    return cleaned, usage


def synthesize_behavioral_profile(behavioral_features, contact_profiles):
    """Phase 4C-3: Generate a behavioral profile via Sonnet.

    Passes both extraction features (with approximate domain-based contact_types)
    and authoritative contact profiles so Sonnet can reconcile.

    Args:
        behavioral_features: List of per-email behavioral dicts from Haiku.
        contact_profiles: List of authoritative contact profile dicts from Phase 4A.

    Returns:
        str: Plain text behavioral profile, or None on failure.
    """
    if not behavioral_features:
        logger.warning("No behavioral features to synthesize")
        return None, {}

    # Format authoritative contact profiles for reconciliation
    profile_lines = []
    for cp in (contact_profiles or []):
        email = cp.get("email", "")
        ctype = cp.get("contact_type", "unknown")
        org = cp.get("inferred_organization", "")
        role = cp.get("inferred_role", "")
        sig = cp.get("significance", "medium")
        profile_lines.append(f"- {email}: {ctype} ({org}, {role}, significance:{sig})")

    profiles_block = ""
    if profile_lines:
        profiles_block = (
            "\n\nAUTHORITATIVE CONTACT PROFILES (use these to reconcile "
            "approximate contact_types in the extraction data):\n"
            + "\n".join(profile_lines)
        )

    prompt_text = (
        f"Behavioral pattern analysis from {len(behavioral_features)} sent emails:\n\n"
        + json.dumps(behavioral_features)
        + profiles_block
    )

    response, usage = call_with_retry(
        prompt=prompt_text,
        system_prompt=SONNET_BEHAVIORAL_PROFILE_PROMPT,
        model="sonnet",
        max_tokens=4096,
        temperature=0.3,
        cache_system_prompt=True,
    )

    if not response:
        logger.error("Behavioral profile synthesis: no response from Sonnet")
        return None, usage

    cleaned = _clean_synthesis_output(response)
    logger.info(f"Generated behavioral profile ({len(cleaned)} chars)")
    return cleaned, usage


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_contact_inputs(contact_freq, response_rates, extractions):
    """Aggregate per-contact data for Sonnet profile synthesis."""
    contacts = {}
    for sender, freq_data in contact_freq.items():
        rates = response_rates.get(sender, {})
        contacts[sender] = {
            "email": sender,
            "emails_received": freq_data["count"],
            "first_seen": freq_data.get("first_seen"),
            "last_seen": freq_data.get("last_seen"),
            "sample_subjects": _clean_subjects(freq_data.get("subjects", []))[:5],
            "to_count": freq_data.get("to_count", 0),
            "cc_count": freq_data.get("cc_count", 0),
            "top_co_recipients": freq_data.get("top_co_recipients", []),
            "response_rate": rates.get("response_rate"),
            "avg_response_time_hours": rates.get("avg_response_time_hours"),
        }

    # Sort by email count, cap at 150 to keep Sonnet's attention focused
    sorted_contacts = sorted(
        contacts.values(), key=lambda c: c["emails_received"], reverse=True
    )
    if len(sorted_contacts) > 150:
        logger.info(f"Contact synthesis: capping at 150 of {len(sorted_contacts)} contacts")
        sorted_contacts = sorted_contacts[:150]
    return sorted_contacts


def _format_contact_prompt(contact_inputs):
    """Format contact data as user message for Sonnet."""
    lines = []
    for i, c in enumerate(contact_inputs, 1):
        lines.append(f"--- CONTACT {i} ---")
        lines.append(f"Email: {c['email']}")
        lines.append(f"Emails received: {c['emails_received']}")
        lines.append(f"First seen: {c.get('first_seen', 'unknown')}")
        lines.append(f"Last seen: {c.get('last_seen', 'unknown')}")
        lines.append(f"TO count: {c.get('to_count', 0)}, CC count: {c.get('cc_count', 0)}")
        if c.get("response_rate") is not None:
            lines.append(f"User response rate: {c['response_rate']:.0%}")
        if c.get("avg_response_time_hours") is not None:
            lines.append(f"Avg response time: {c['avg_response_time_hours']:.1f} hours")
        subjects = c.get("sample_subjects", [])
        if subjects:
            lines.append(f"Sample subjects: {'; '.join(subjects)}")
        co_recip = c.get("top_co_recipients", [])
        if co_recip:
            lines.append(f"Common co-recipients: {', '.join(co_recip)}")
        lines.append("")

    return "\n".join(lines)


_REPLY_PREFIX_RE = re.compile(r'^(?:RE|FW|FWD)\s*:\s*', re.IGNORECASE)


def _clean_subjects(subjects):
    """Strip RE/FW prefixes and deduplicate subject lines."""
    seen = set()
    cleaned = []
    for subj in subjects:
        s = _REPLY_PREFIX_RE.sub('', subj).strip()
        # Repeat in case of nested prefixes (RE: RE: FW: ...)
        while _REPLY_PREFIX_RE.match(s):
            s = _REPLY_PREFIX_RE.sub('', s).strip()
        key = s.lower()
        if key and key not in seen:
            seen.add(key)
            cleaned.append(s)
    return cleaned


_PREAMBLE_PATTERNS = [
    re.compile(r"^(?:Here(?:'s| is) (?:the |a |your )?(?:writing |behavioral )?.+?(?:guide|profile|analysis).+?:\s*\n)", re.IGNORECASE),
    re.compile(r"^(?:Based on (?:the |my )?analysis.+?:\s*\n)", re.IGNORECASE),
    re.compile(r"^(?:After (?:analyzing|reviewing).+?:\s*\n)", re.IGNORECASE),
]

_META_COMMENTARY_PATTERNS = [
    re.compile(r"\n\s*(?:Note|Caveat|Disclaimer|Important):?\s.+$", re.IGNORECASE | re.DOTALL),
    re.compile(r"\n\s*(?:This (?:profile|guide) (?:is |was )(?:based on|generated|derived).+)$", re.IGNORECASE | re.DOTALL),
    re.compile(r"\n\s*(?:\*\*?Note\*?\*?:?\s.+)$", re.IGNORECASE | re.DOTALL),
]


def _clean_synthesis_output(text):
    """Strip markdown fencing, preambles, and trailing meta-commentary."""
    if not text:
        return text

    result = text.strip()

    # Strip markdown fencing
    if result.startswith("```"):
        lines = result.split("\n")
        if lines[-1].strip().startswith("```"):
            result = "\n".join(lines[1:-1])
        else:
            result = "\n".join(lines[1:])
        result = result.strip()

    # Strip common preamble patterns
    for pattern in _PREAMBLE_PATTERNS:
        result = pattern.sub('', result).strip()

    # Strip trailing meta-commentary
    for pattern in _META_COMMENTARY_PATTERNS:
        result = pattern.sub('', result).strip()

    return result
