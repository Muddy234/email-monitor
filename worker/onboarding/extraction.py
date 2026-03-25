"""Phase 3 + 4C-1: Haiku-powered extraction for onboarding.

Phase 3: Extract topic keywords, email types, deadlines from received emails.
Phase 4C-1: Extract writing style patterns from sent emails.

Uses ThreadPoolExecutor for concurrent batch processing.
"""

import json
import logging
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from onboarding.collectors import clean_email_body
from onboarding.prompts import (
    HAIKU_EXTRACTION_PROMPT,
    HAIKU_STYLE_EXTRACTION_PROMPT,
    HAIKU_BEHAVIORAL_EXTRACTION_PROMPT,
)
from onboarding.retry import call_with_retry
from onboarding.stats_extraction import _infer_external_type

logger = logging.getLogger("worker.onboarding")

BATCH_SIZE = 20
MAX_WORKERS = 5


def _parse_json_response(text):
    """Parse JSON from an LLM response, stripping markdown fences if present."""
    if not text:
        return None
    stripped = text.strip()
    # Remove ```json ... ``` or ``` ... ``` wrappers
    if stripped.startswith("```"):
        # Find end of first line (skip ```json or ```)
        first_newline = stripped.index("\n")
        last_fence = stripped.rfind("```")
        if last_fence > first_newline:
            stripped = stripped[first_newline + 1:last_fence].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fallback: find first { and last } to handle preamble/postamble text
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(stripped[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    return None


def extract_email_features(received, contact_freq):
    """Phase 3: Extract topic keywords and metadata via Haiku.

    Selects the most important emails, batches them, and runs concurrent
    Haiku calls to extract structured features.

    Args:
        received: List of received email dicts.
        contact_freq: Contact frequency dict from Phase 2A.

    Returns:
        dict with 'extractions' (list) and 'keyword_frequencies' (Counter).
    """
    # Select emails to extract — prioritize high-frequency senders + replied-to
    selected = _select_emails_for_extraction(received, contact_freq)
    logger.info(f"Selected {len(selected)} emails for Haiku extraction")

    # Prepare batches (no To/CC — topic keywords come from subject + body)
    batches = _prepare_batches(selected, BATCH_SIZE, include_recipients=False)
    total_batches = len(batches)
    completed_batches = 0
    all_extractions = []
    failed_batches = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_run_extraction_batch, batch, i): i
            for i, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            batch_idx = futures[future]
            try:
                result = future.result()
                if result:
                    all_extractions.extend(result)
                else:
                    failed_batches += 1
            except Exception:
                logger.exception(f"Extraction batch {batch_idx} failed")
                failed_batches += 1

            completed_batches += 1

    # Halt if too many failures
    if total_batches > 0 and failed_batches / total_batches > 0.5:
        logger.error(
            f"Extraction failed: {failed_batches}/{total_batches} batches failed"
        )
        return None

    # Aggregate keyword frequencies
    keyword_freq = Counter()
    for ext in all_extractions:
        for kw in ext.get("topic_keywords", []):
            keyword_freq[kw.lower()] += 1

    logger.info(
        f"Extracted features from {len(all_extractions)} emails, "
        f"{len(keyword_freq)} unique keywords"
    )

    return {
        "extractions": all_extractions,
        "keyword_frequencies": dict(keyword_freq.most_common(200)),
    }


def sample_unified_sent_emails(sent_emails, user_domain, max_count=120):
    """Stratified sample by body_length x contact_type (3x2 grid).

    Both style and behavioral extraction use this same sample to ensure
    profile agreement.
    """
    if len(sent_emails) <= max_count:
        return list(sent_emails)

    import re as _re

    buckets = {}  # (length_tier, contact_type) -> [emails]
    for email in sent_emails:
        body_len = len(email.get("body") or "")
        if body_len < 100:
            length_tier = "short"
        elif body_len < 500:
            length_tier = "medium"
        else:
            length_tier = "long"

        to_field = (email.get("to_field") or "").lower()
        addrs = _re.findall(r"[\w.+-]+@[\w.-]+\.\w+", to_field)
        first_addr = addrs[0] if addrs else ""
        ctype = _infer_contact_type(first_addr, user_domain)

        buckets.setdefault((length_tier, ctype), []).append(email)

    total = len(sent_emails)
    result = []
    for key, bucket in buckets.items():
        random.shuffle(bucket)
        share = max(3, int(max_count * len(bucket) / total))
        result.extend(bucket[:share])

    random.shuffle(result)
    return result[:max_count]


def extract_writing_styles(sent_emails, pre_sampled=None):
    """Phase 4C-1: Extract writing style patterns from sent emails via Haiku.

    Args:
        sent_emails: List of sent email dicts.
        pre_sampled: Optional pre-sampled list (skips internal sampling).

    Returns:
        dict with 'style_features' (list) and 'sample_count' (int),
        or None if extraction fails.
    """
    if pre_sampled is None:
        raise ValueError("pre_sampled is required (use sample_unified_sent_emails)")
    sampled = pre_sampled
    logger.info(f"Sampled {len(sampled)} sent emails for style extraction")

    if not sampled:
        return {"style_features": [], "sample_count": 0}

    batches = _prepare_batches(sampled, BATCH_SIZE)
    all_features = []
    failed_batches = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_run_style_batch, batch, i): i
            for i, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    all_features.extend(result)
                else:
                    failed_batches += 1
            except Exception:
                logger.exception("Style extraction batch failed")
                failed_batches += 1

    if not all_features:
        logger.warning("No style features extracted")
        return None

    logger.info(f"Extracted style features from {len(all_features)} sent emails")

    return {
        "style_features": all_features,
        "sample_count": len(sampled),
    }


# Smaller than BATCH_SIZE because each behavioral pair includes parent body +
# sent body + metadata context — significantly more tokens per item.
BEHAVIORAL_BATCH_SIZE = 10
BEHAVIORAL_MAX_SAMPLE = 160
PARENT_TRUNCATION = 1500


def extract_behavioral_features(sent_emails, response_events, received_emails,
                                user_domain=None, pre_sampled=None):
    """Phase 4C-1b: Extract behavioral patterns from sent emails via Haiku.

    Pairs sent emails with their parent inbound messages using response_events
    linkage.

    Args:
        sent_emails: List of sent email dicts.
        response_events: List of response event dicts from Phase 2.
        received_emails: List of received email dicts.
        user_domain: User's email domain for contact_type heuristic.
        pre_sampled: Optional pre-sampled list (skips internal sampling).

    Returns:
        dict with 'behavioral_features' (list) and 'sample_count' (int),
        or None if extraction fails.
    """
    # Build pairing map: sent_msg_id -> received email
    received_by_id = {}
    for email in received_emails:
        eid = email.get("email_ref") or email.get("id")
        if eid:
            received_by_id[eid] = email

    # Map sent emails to their parent inbound via response_events
    # response_events link received -> sent via response_msg_id
    sent_to_parent = {}
    for event in response_events:
        resp_id = event.get("response_msg_id")
        recv_id = event.get("email_ref") or event.get("email_id")
        if resp_id and recv_id and recv_id in received_by_id:
            sent_to_parent[resp_id] = received_by_id[recv_id]

    # Build sent email lookup by id
    sent_by_id = {}
    for email in sent_emails:
        eid = email.get("email_ref") or email.get("id")
        if eid:
            sent_by_id[eid] = email

    if pre_sampled is None:
        raise ValueError("pre_sampled is required (use sample_unified_sent_emails)")
    sampled = pre_sampled
    logger.info(f"Sampled {len(sampled)} sent emails for behavioral extraction")

    if not sampled:
        return {"behavioral_features": [], "sample_count": 0}

    # Format pairs into batches (with response event metadata)
    batches = _prepare_behavioral_batches(
        sampled, sent_to_parent, sent_by_id, user_domain,
        response_events=response_events,
    )
    all_features = []
    failed_batches = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_run_behavioral_batch, batch, i): i
            for i, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    all_features.extend(result)
                else:
                    failed_batches += 1
            except Exception:
                logger.exception("Behavioral extraction batch failed")
                failed_batches += 1

    if not all_features:
        logger.warning("No behavioral features extracted")
        return None

    logger.info(f"Extracted behavioral features from {len(all_features)} sent emails")

    return {
        "behavioral_features": all_features,
        "sample_count": len(sampled),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _select_emails_for_extraction(received, contact_freq, max_emails=300):
    """Select the most important emails for extraction.

    Priority: top senders by frequency, then fill with random.
    """
    if len(received) <= max_emails:
        return received

    # Sort senders by frequency
    sorted_senders = sorted(
        contact_freq.items(), key=lambda x: x[1]["count"], reverse=True
    )
    top_senders = {s for s, _ in sorted_senders[:30]}

    priority = []
    rest = []

    for email in received:
        sender = (email.get("sender_email") or "").lower()
        if sender in top_senders:
            priority.append(email)
        else:
            rest.append(email)

    # Fill remaining slots randomly
    remaining = max_emails - len(priority)
    if remaining > 0 and rest:
        random.shuffle(rest)
        priority.extend(rest[:remaining])

    return priority[:max_emails]



def _prepare_batches(emails, batch_size, include_recipients=True):
    """Split emails into batches and format for LLM input.

    Args:
        include_recipients: Include To/CC fields. False for Phase 3 topic
            extraction where recipients don't affect keyword quality.
    """
    batches = []
    for i in range(0, len(emails), batch_size):
        chunk = emails[i:i + batch_size]
        formatted = []
        for j, email in enumerate(chunk):
            body = clean_email_body(email.get("body") or "")
            lines = [
                f"--- EMAIL {j + 1} ---",
                f"From: {email.get('sender_email') or email.get('sender', 'unknown')}",
            ]
            if include_recipients:
                lines.append(f"To: {email.get('to_field', '')}")
                lines.append(f"CC: {email.get('cc_field', '')}")
            lines.extend([
                f"Subject: {email.get('subject', '(no subject)')}",
                f"Date: {email.get('received_time', '')}",
                f"Body:\n{body}",
            ])
            formatted.append("\n".join(lines))
        batches.append("\n\n".join(formatted))
    return batches


def _run_extraction_batch(batch_text, batch_idx):
    """Run a single Haiku extraction batch. Returns list of extractions."""
    response, _usage = call_with_retry(
        prompt=batch_text,
        system_prompt=HAIKU_EXTRACTION_PROMPT,
        model="haiku",
        max_tokens=4096,
        temperature=0,
        cache_system_prompt=True,
    )
    if not response:
        logger.warning(f"Extraction batch {batch_idx}: no response")
        return None

    data = _parse_json_response(response)
    if data is None:
        logger.warning(f"Extraction batch {batch_idx}: invalid JSON — {response[:200]}")
        return None
    return data.get("extractions", [])


def _run_style_batch(batch_text, batch_idx):
    """Run a single Haiku style extraction batch. Returns list of features."""
    response, _usage = call_with_retry(
        prompt=batch_text,
        system_prompt=HAIKU_STYLE_EXTRACTION_PROMPT,
        model="haiku",
        max_tokens=4096,
        temperature=0,
        cache_system_prompt=True,
    )
    if not response:
        logger.warning(f"Style batch {batch_idx}: no response")
        return None

    data = _parse_json_response(response)
    if data is None:
        logger.warning(f"Style batch {batch_idx}: invalid JSON — {response[:200]}")
        return None
    return data.get("extractions", [])


# ---------------------------------------------------------------------------
# Behavioral extraction helpers
# ---------------------------------------------------------------------------

def _infer_contact_type(email_addr, user_domain):
    """Domain-based contact_type heuristic (approximate).

    Same org domain = internal_colleague, different = external subtype
    inferred from domain keywords (external_lender, external_legal, etc.).
    """
    if not email_addr or not user_domain:
        return "external"
    try:
        domain = email_addr.split("@")[1].lower()
        if domain == user_domain.lower():
            return "internal_colleague"
        return _infer_external_type(domain)
    except (IndexError, AttributeError):
        return "external"



def _prepare_behavioral_batches(sampled, sent_to_parent, sent_by_id,
                                user_domain, response_events=None):
    """Format sent+parent pairs into batches for Haiku."""
    import re as _re

    # Build sent_id -> response_event for metadata injection
    sent_id_to_event = {}
    if response_events:
        for event in response_events:
            resp_id = event.get("response_msg_id")
            if resp_id:
                sent_id_to_event[resp_id] = event

    batches = []
    batch_lines = []
    count_in_batch = 0
    pair_index = 1

    for email in sampled:
        eid = email.get("email_ref") or email.get("id")
        parent = sent_to_parent.get(eid) if eid else None

        # Determine contact_type
        to_field = (email.get("to_field") or "").lower()
        addrs = _re.findall(r"[\w.+-]+@[\w.-]+\.\w+", to_field)
        first_addr = addrs[0] if addrs else ""
        ctype = _infer_contact_type(first_addr, user_domain)

        # Build metadata context from response event
        re_meta = sent_id_to_event.get(eid, {})
        meta_parts = []
        latency = re_meta.get("response_latency_hours")
        if latency is not None:
            meta_parts.append(f"response_latency_hours: {latency:.1f}")
        if re_meta.get("has_question") is not None:
            meta_parts.append(f"inbound_has_question: {re_meta['has_question']}")
        if re_meta.get("has_action_language") is not None:
            meta_parts.append(f"inbound_has_action_language: {re_meta['has_action_language']}")
        if re_meta.get("subject_type"):
            meta_parts.append(f"subject_type: {re_meta['subject_type']}")
        if re_meta.get("thread_depth") is not None:
            meta_parts.append(f"thread_depth: {re_meta['thread_depth']}")
        meta_block = f"[CONTEXT: {', '.join(meta_parts)}]\n" if meta_parts else ""

        sent_body = clean_email_body(email.get("body") or "")

        if parent:
            parent_body = clean_email_body(parent.get("body") or "")
            if len(parent_body) > PARENT_TRUNCATION:
                parent_body = parent_body[:PARENT_TRUNCATION] + "\n[... truncated]"
            sender_name = parent.get("sender_name") or parent.get("sender_email") or "Unknown"
            text = (
                f"--- PAIR {pair_index} ---\n"
                f"INBOUND (from {sender_name}, contact_type: {ctype}):\n"
                f"{meta_block}"
                f"{parent_body}\n\n"
                f"USER'S REPLY:\n"
                f"{sent_body}\n"
            )
        else:
            text = (
                f"--- EMAIL {pair_index} ---\n"
                f"SENT TO: contact_type: {ctype}\n"
                f"{meta_block}"
                f"{sent_body}\n"
            )

        batch_lines.append(text)
        count_in_batch += 1
        pair_index += 1

        if count_in_batch >= BEHAVIORAL_BATCH_SIZE:
            batches.append("\n".join(batch_lines))
            batch_lines = []
            count_in_batch = 0

    if batch_lines:
        batches.append("\n".join(batch_lines))

    return batches


def _run_behavioral_batch(batch_text, batch_idx):
    """Run a single Haiku behavioral extraction batch."""
    response, _usage = call_with_retry(
        prompt=batch_text,
        system_prompt=HAIKU_BEHAVIORAL_EXTRACTION_PROMPT,
        model="haiku",
        max_tokens=4096,
        temperature=0,
        cache_system_prompt=True,
    )
    if not response:
        logger.warning(f"Behavioral batch {batch_idx}: no response")
        return None

    data = _parse_json_response(response)
    if data is None:
        logger.warning(f"Behavioral batch {batch_idx}: invalid JSON — {response[:200]}")
        return None
    return data.get("extractions", [])
