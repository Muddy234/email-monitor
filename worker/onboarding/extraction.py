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
from onboarding.prompts import HAIKU_EXTRACTION_PROMPT, HAIKU_STYLE_EXTRACTION_PROMPT
from onboarding.retry import call_with_retry

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

    # Prepare batches
    batches = _prepare_batches(selected, BATCH_SIZE)
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


def extract_writing_styles(sent_emails):
    """Phase 4C-1: Extract writing style patterns from sent emails via Haiku.

    Samples up to 50 sent emails (stratified by length), extracts
    greeting, signoff, tone, phrases.

    Args:
        sent_emails: List of sent email dicts.

    Returns:
        dict with 'style_features' (list) and 'sample_count' (int),
        or None if extraction fails.
    """
    sampled = _sample_sent_emails(sent_emails, max_count=50)
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


def _sample_sent_emails(sent_emails, max_count=50):
    """Stratified sample of sent emails by body length."""
    if len(sent_emails) <= max_count:
        return sent_emails

    # Stratify: short (<100 chars), medium (100-500), long (500+)
    short, medium, long_ = [], [], []
    for email in sent_emails:
        body_len = len(email.get("body") or "")
        if body_len < 100:
            short.append(email)
        elif body_len < 500:
            medium.append(email)
        else:
            long_.append(email)

    # Allocate proportionally, minimum 5 per bucket if available
    result = []
    for bucket in (short, medium, long_):
        random.shuffle(bucket)
        share = max(5, int(max_count * len(bucket) / len(sent_emails)))
        result.extend(bucket[:share])

    random.shuffle(result)
    return result[:max_count]


def _prepare_batches(emails, batch_size):
    """Split emails into batches and format for LLM input."""
    batches = []
    for i in range(0, len(emails), batch_size):
        chunk = emails[i:i + batch_size]
        formatted = []
        for j, email in enumerate(chunk):
            body = clean_email_body(email.get("body") or "")
            text = (
                f"--- EMAIL {j + 1} ---\n"
                f"From: {email.get('sender_email') or email.get('sender', 'unknown')}\n"
                f"To: {email.get('to_field', '')}\n"
                f"CC: {email.get('cc_field', '')}\n"
                f"Subject: {email.get('subject', '(no subject)')}\n"
                f"Date: {email.get('received_time', '')}\n"
                f"Body:\n{body}\n"
            )
            formatted.append(text)
        batches.append("\n".join(formatted))
    return batches


def _run_extraction_batch(batch_text, batch_idx):
    """Run a single Haiku extraction batch. Returns list of extractions."""
    response = call_with_retry(
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
    response = call_with_retry(
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
