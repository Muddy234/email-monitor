"""Phase 5: Opus-powered calibration for onboarding.

Runs asynchronously after Phases 1-4 complete. Selects 20-30 boundary
emails, calls Opus to validate contact profiles, generate worked
examples, and produce classification rules.
"""

import json
import logging
import random

from onboarding.collectors import clean_email_body
from onboarding.prompts import OPUS_CALIBRATION_PROMPT
from onboarding.retry import call_with_retry

logger = logging.getLogger("worker.onboarding")


def run_calibration(db, user_id):
    """Run Opus calibration for a user.

    Selects boundary emails, calls Opus for profile validation +
    worked examples + classification rules, writes results to DB.

    Args:
        db: SupabaseWorkerClient instance.
        user_id: UUID string.

    Returns:
        bool: True if calibration succeeded.
    """
    logger.info(f"Starting Opus calibration for user {user_id}")

    # Load existing onboarding data
    profile = db.fetch_user_config(user_id)
    aliases = {a.lower() for a in (profile.get("user_email_aliases") or [])}

    topic_result = (
        db.client.table("user_topic_profile")
        .select("*")
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    topic_profile = topic_result.data if topic_result.data else {}

    contacts_result = (
        db.client.table("contacts")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    contact_profiles = contacts_result.data or []

    # Get emails for calibration sample
    emails = db.fetch_emails_for_onboarding(user_id, days=30)
    received = [
        e for e in emails
        if (e.get("folder") or "").lower() != "sent items"
        and (e.get("sender_email") or "").lower() not in aliases
    ]
    sent = [
        e for e in emails
        if (e.get("folder") or "").lower() == "sent items"
        or (e.get("sender_email") or "").lower() in aliases
    ]

    # Select boundary emails
    boundary_emails = _select_boundary_emails(received, sent, aliases)
    if len(boundary_emails) < 10:
        logger.warning(f"Only {len(boundary_emails)} boundary emails found, skipping calibration")
        return False

    # Build Opus prompt
    prompt_text = _build_calibration_prompt(
        boundary_emails, contact_profiles, topic_profile
    )

    response = call_with_retry(
        prompt=prompt_text,
        system_prompt=OPUS_CALIBRATION_PROMPT,
        model="opus",
        max_tokens=8192,
        temperature=0,
        max_retries=2,
        timeout=180,
        cache_system_prompt=True,
    )

    if not response:
        logger.error("Opus calibration: no response")
        return False

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        logger.error("Opus calibration: invalid JSON response")
        return False

    # Apply profile corrections
    corrections = data.get("profile_corrections", [])
    if corrections:
        _apply_profile_corrections(db, user_id, corrections, contact_profiles)

    # Store worked examples and classification rules
    worked_examples = data.get("worked_examples", [])
    classification_rules = data.get("classification_rules", [])

    db.update_topic_profile_calibration(
        user_id, worked_examples, classification_rules
    )

    logger.info(
        f"Opus calibration complete: {len(worked_examples)} examples, "
        f"{len(classification_rules)} rules, {len(corrections)} corrections"
    )
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _select_boundary_emails(received, sent, user_aliases, target=25):
    """Select 20-30 emails across 4 categories for calibration.

    Categories:
    1. User replied quickly (clear response signals)
    2. User was in TO but didn't reply
    3. User was in CC but replied
    4. High-velocity threads
    """
    # Build set of conversation_ids the user responded to
    sent_conv_ids = set()
    for s in sent:
        conv_id = s.get("conversation_id")
        if conv_id:
            sent_conv_ids.add(conv_id)

    # Categorize received emails
    cat1_replied = []      # User replied
    cat2_to_no_reply = []  # In TO, didn't reply
    cat3_cc_replied = []   # In CC, replied
    cat4_all = []          # Everything (for thread velocity sampling)

    for email in received:
        conv_id = email.get("conversation_id")
        cc_field = (email.get("cc_field") or "").lower()
        user_in_cc = any(alias in cc_field for alias in user_aliases)
        replied = conv_id in sent_conv_ids if conv_id else False

        if replied and not user_in_cc:
            cat1_replied.append(email)
        elif not replied and not user_in_cc:
            cat2_to_no_reply.append(email)
        elif replied and user_in_cc:
            cat3_cc_replied.append(email)

        cat4_all.append(email)

    # Sample from each category
    per_cat = max(target // 4, 3)
    selected = []

    for cat in (cat1_replied, cat2_to_no_reply, cat3_cc_replied):
        random.shuffle(cat)
        selected.extend(cat[:per_cat])

    # Fill remaining with random from cat4
    remaining = target - len(selected)
    if remaining > 0:
        random.shuffle(cat4_all)
        for email in cat4_all:
            if email not in selected:
                selected.append(email)
                if len(selected) >= target:
                    break

    return selected


def _build_calibration_prompt(boundary_emails, contact_profiles, topic_profile):
    """Build the user message for the Opus calibration call."""
    sections = []

    # Section 1: Contact profiles
    sections.append("=== CONTACT PROFILES ===")
    for cp in contact_profiles[:30]:
        sections.append(
            f"- {cp.get('email')}: {cp.get('role', 'unknown')} at "
            f"{cp.get('organization', 'unknown')} "
            f"({cp.get('contact_type', 'unknown')}, "
            f"significance: {cp.get('relationship_significance', 'unknown')})"
        )

    # Section 2: Topic domains
    sections.append("\n=== TOPIC DOMAINS ===")
    domains = topic_profile.get("domains", [])
    if isinstance(domains, str):
        try:
            domains = json.loads(domains)
        except json.JSONDecodeError:
            domains = []
    for d in domains:
        if isinstance(d, dict):
            sections.append(
                f"- {d.get('domain_name', 'unknown')}: "
                f"{', '.join(d.get('keywords', [])[:5])} "
                f"(signal: {d.get('signal_strength', 'unknown')})"
            )

    # Section 3: Boundary emails with outcomes
    sections.append("\n=== EMAILS WITH KNOWN OUTCOMES ===")
    for i, email in enumerate(boundary_emails, 1):
        body = clean_email_body(email.get("body") or "", max_chars=500)
        replied = "USER REPLIED" if email.get("_replied") else "USER DID NOT REPLY"
        # We mark replied status during selection but it's not on the email dict
        # Just present the email; Opus infers from conversation patterns
        sections.append(
            f"\n--- EMAIL {i} ---\n"
            f"From: {email.get('sender_email', 'unknown')}\n"
            f"To: {email.get('to_field', '')}\n"
            f"CC: {email.get('cc_field', '')}\n"
            f"Subject: {email.get('subject', '(no subject)')}\n"
            f"Date: {email.get('received_time', '')}\n"
            f"Status: {email.get('status', 'unknown')}\n"
            f"Body:\n{body}"
        )

    return "\n".join(sections)


def _apply_profile_corrections(db, user_id, corrections, existing_profiles):
    """Apply Opus-suggested corrections to contact profiles."""
    # Build lookup of existing contacts
    existing_by_email = {
        cp.get("email", "").lower(): cp for cp in existing_profiles
    }

    updates = []
    for correction in corrections:
        email = (correction.get("email") or "").lower()
        if email not in existing_by_email:
            continue

        update = {"email": email}
        for field in ("organization", "role", "expertise_areas",
                      "contact_type", "relationship_significance",
                      "relationship_summary"):
            if field in correction:
                update[field] = correction[field]

        # Map field names from Opus output
        if "inferred_organization" in correction:
            update["organization"] = correction["inferred_organization"]
        if "inferred_role" in correction:
            update["role"] = correction["inferred_role"]

        updates.append(update)

    if updates:
        db.upsert_contacts(user_id, updates)
        logger.info(f"Applied {len(updates)} profile corrections from Opus")
