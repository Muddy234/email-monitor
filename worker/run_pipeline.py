"""Pipeline orchestration for the Railway worker.

Ports the core logic from pipeline/runner.py, adapted for Supabase I/O:
- Reads email data from Supabase rows (not from a provider).
- Writes results to Supabase tables (not to Outlook via provider).
- Reuses existing EmailFilter, ClaudeAnalyzer, DraftGenerator classes.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

from collections import defaultdict

from pipeline.filter import EmailFilter
from pipeline.analyzer import ClaudeAnalyzer
from pipeline.drafts import DraftGenerator
from pipeline.prompts import get_draft_prompt_template
from pipeline.scorer import UserScoringArtifacts, score_email, check_triage_gate
from pipeline.enrichment import assemble_enrichment

logger = logging.getLogger("worker")

# Per-user artifact cache: user_id → (artifacts, loaded_at)
_user_artifacts_cache = {}
_ARTIFACTS_CACHE_TTL = 300  # 5 min


def _get_user_artifacts(db, user_id):
    """Load per-user scoring artifacts from DB with 5-min cache."""
    import time
    now = time.time()

    cached = _user_artifacts_cache.get(user_id)
    if cached and (now - cached[1]) < _ARTIFACTS_CACHE_TTL:
        return cached[0]

    params = db.fetch_scoring_parameters(user_id)
    if params:
        artifacts = UserScoringArtifacts(params)
    else:
        # Fall back to defaults for users without trained model
        from onboarding.model_trainer import DEFAULT_PARAMETERS
        artifacts = UserScoringArtifacts(DEFAULT_PARAMETERS)
        logger.info(f"  User {user_id[:8]}...: using default scoring parameters")

    _user_artifacts_cache[user_id] = (artifacts, now)
    return artifacts


def build_config_from_profile(profile):
    """Convert a Supabase profiles row into the config dict that
    EmailFilter, ClaudeAnalyzer, and DraftGenerator expect.

    The existing pipeline classes read config keys like
    'filter_blacklist_senders', etc.
    Profile rows store per-user settings; env vars fill in API keys.

    Args:
        profile: dict from profiles table.

    Returns:
        dict: Config dict compatible with existing pipeline classes.
    """
    config = {
        # Provider settings (not used by worker, but kept for compatibility)
        "email_provider": "supabase",
        "process_flagged_only": profile.get("process_flagged_only", True),
        "max_emails_to_scan": profile.get("max_emails_to_scan", 500),

        # User identity
        "user_email_aliases": profile.get("user_email_aliases", []),

        # Claude settings — from env vars (not stored in user profile)
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "classification_model": os.environ.get("CLASSIFICATION_MODEL", "haiku"),
        "draft_model": os.environ.get("DRAFT_MODEL", "sonnet"),
        "claude_cli_timeout_seconds": int(os.environ.get("CLAUDE_TIMEOUT", "120")),
        "max_body_chars": int(os.environ.get("MAX_BODY_CHARS", "8000")),

        # Draft settings
        "draft_user_name": os.environ.get("DRAFT_USER_NAME", ""),
        "draft_user_title": os.environ.get("DRAFT_USER_TITLE", ""),

        # Pipeline feature flags
        "enable_email_filtering": True,
        "enable_claude_analysis": True,
        "enable_draft_generation": True,
        "enable_conversation_grouping": True,
        "max_emails_per_run": int(os.environ.get("MAX_EMAILS_PER_RUN", "20")),

        # Filter settings
        "filter_blacklist_senders": [
            "noreply@",
            "no-reply@",
            "mailer-daemon@",
            "postmaster@",
            "notifications@github.com",
            "builds@travis-ci.org",
            "jira@",
            "calendar-notification@google.com",
            "notify@",
            "donotreply@",
            "automated@",
            "newsletter@",
            "marketing@",
            "updates@",
        ],
        "filter_blacklist_subject_patterns": [
            "accepted:",
            "declined:",
            "tentatively accepted:",
            "canceled:",
            "undeliverable:",
            "out of office",
            "automatic reply",
            "auto-reply",
            "delivery status notification",
            "read receipt:",
            "unsubscribe confirmation",
        ],
        "filter_whitelist_senders": [],
        "filter_whitelist_domains": [],
        "filter_project_keywords": [
            "thomas ranch",
            "turtle bay",
            "north shore",
            "loraloma",
            "lore loma",
            "kaikani",
            "wasatch highlands",
            "ocean club",
            "zone 8",
            "hc2",
            "rr3",
        ],
        "filter_auto_important_patterns": ["/o=exchangelabs/"],
        "filter_direct_recipient": "",
    }

    return config


def filter_emails(db_client, emails, user_id, config):
    """Filter and signal-enrich a batch of emails, writing skip results to DB.

    Returns list of email_data dicts that passed filtering (with _db_id set).
    Skipped emails are marked completed and classified in the DB immediately.
    """
    email_filter = EmailFilter(config)
    user_aliases = [a.lower() for a in config.get("user_email_aliases", []) if a]

    filtered = []
    skip_ids = []
    skip_classifications = []

    for email in emails:
        email_data = supabase_row_to_email_data(email)
        email_data["signals"] = build_signals(email_data, user_aliases)
        classification = email_filter.classify(email_data)

        if classification == "skip":
            logger.info(f"  Skipped (filter): {email_data.get('subject', '?')[:60]}")
            skip_ids.append(email["id"])
            skip_classifications.append({
                "email_id": email["id"],
                "user_id": user_id,
                "needs_response": False,
                "action": "skip",
                "context": "Filtered out by rules",
                "project": "",
                "priority": 0,
            })
            continue

        skip_reason = _should_auto_skip(email_data, email_data["signals"])
        if skip_reason:
            logger.info(f"  Auto-skipped ({skip_reason}): {email_data.get('subject', '?')[:60]}")
            skip_ids.append(email["id"])
            skip_classifications.append({
                "email_id": email["id"],
                "user_id": user_id,
                "needs_response": False,
                "action": "Auto-skipped by signal rules",
                "context": f"Skipped: {skip_reason}",
                "project": "",
                "priority": 0,
            })
            continue

        email_data["_filter_result"] = classification
        email_data["_db_id"] = email["id"]
        filtered.append(email_data)

    # Batch-write all skipped emails in bulk
    if skip_ids:
        db_client.bulk_update_email_status(skip_ids, "completed")
        db_client.bulk_insert_classifications(skip_classifications)
        logger.info(f"  Batch-wrote {len(skip_ids)} skipped emails")

    return filtered


def process_classification_results(db_client, action_items, filtered_emails,
                                   user_id, config, draft_generator):
    """Write classification results to DB and collect draft candidates.

    Returns:
        tuple: (emails_processed count, list of draft_candidate dicts)
            Each draft_candidate has keys: db_id, email_data, action_context
    """
    email_index_map = {i + 1: ed for i, ed in enumerate(filtered_emails)}
    draft_max_age_hours = int(os.environ.get("DRAFT_MAX_AGE_HOURS", "24"))
    emails_processed = 0
    draft_candidates = []

    for item in action_items:
        email_idx = item.get("email_index", 0)
        email_data = email_index_map.get(email_idx)
        if not email_data:
            continue

        db_id = email_data["_db_id"]
        needs_response = item.get("needs_response", False)

        # Build classification data — support both old (action/context) and new (reason/archetype/confidence) schemas
        classification = {
            "needs_response": needs_response,
            "action": item.get("action", ""),
            "context": item.get("context", ""),
            "project": item.get("project", ""),
            "priority": _parse_priority(item.get("priority", "")),
        }

        # New enriched schema fields
        if "reason" in item:
            classification["reason"] = item["reason"]
        if "archetype" in item:
            classification["archetype"] = item["archetype"]
        if "confidence" in item:
            classification["classification_confidence"] = item["confidence"]

        # For enriched results, populate action/context from reason if missing
        if not classification["action"] and item.get("reason"):
            classification["action"] = item["reason"]
        if not classification["context"] and item.get("reason"):
            classification["context"] = item["reason"]

        db_client.insert_classification(db_id, user_id, classification)

        email_age_ok = _is_recent(email_data.get("received_time"), draft_max_age_hours)
        if not email_age_ok and needs_response:
            logger.info(f"  Skipping draft (too old): {email_data.get('subject', '?')[:60]}")

        if needs_response and email_age_ok and config.get("enable_draft_generation", True):
            action_context = {
                "action": item.get("action", ""),
                "context": item.get("context", ""),
            }

            # Pass enriched fields to draft generator
            if "reason" in item:
                action_context["reason"] = item["reason"]
            if "archetype" in item:
                action_context["archetype"] = item["archetype"]

            # Pass enrichment data if available on the email
            if email_data.get("_enrichment"):
                action_context["enrichment"] = email_data["_enrichment"]

            conv_id = email_data.get("conversation_id")
            if conv_id:
                conv_messages = db_client.fetch_conversation_context(user_id, conv_id)
                if conv_messages:
                    action_context["conversation_history"] = conv_messages

            draft_candidates.append({
                "db_id": db_id,
                "email_data": email_data,
                "action_context": action_context,
            })

        db_client.update_email_status(db_id, "completed")
        emails_processed += 1

    # Mark any remaining filtered emails that didn't get an action item
    for ed in filtered_emails:
        try:
            db_client.update_email_status(ed["_db_id"], "completed")
        except Exception:
            pass

    return emails_processed, draft_candidates


def build_signals(email_data, user_aliases):
    """Build response signals from email data and user aliases.

    Computes signals 1-5 from the strategy doc using data already on the
    email row. Signals that need external data (thread history, response
    rate, thread velocity) fall back to null/insufficient-data states that
    the analyzer already handles gracefully.

    Args:
        email_data: dict from supabase_row_to_email_data().
        user_aliases: list[str] of the user's email addresses (lowercase).

    Returns:
        dict: Signals block compatible with analyzer._format_signal_block().
    """
    to_field = (email_data.get("to_field") or "").lower()
    cc_field = (email_data.get("cc_field") or "").lower()
    recipients = email_data.get("recipients") or []
    body = email_data.get("body") or ""
    subject = email_data.get("subject") or ""

    # --- Signal 1: User position + recipient counts ---
    to_addrs = [r["address"].lower() for r in recipients if r.get("type") == 1 and r.get("address")]
    cc_addrs = [r["address"].lower() for r in recipients if r.get("type") == 2 and r.get("address")]

    user_position = "UNKNOWN"
    for alias in user_aliases:
        if alias in to_field or alias in [a for a in to_addrs]:
            user_position = "TO"
            break
        if alias in cc_field or alias in [a for a in cc_addrs]:
            user_position = "CC"
            break

    to_count = len(to_addrs)
    cc_count = len(cc_addrs)
    total_recipients = to_count + cc_count

    # --- Signal 2: Name mention ---
    # Extract name parts from aliases (e.g. "nate.mcbride@..." → "nate", "mcbride")
    name_tokens = set()
    for alias in user_aliases:
        local = alias.split("@")[0]
        # Split on dots, underscores, hyphens
        parts = re.split(r'[._\-]', local)
        for p in parts:
            if len(p) >= 3:
                name_tokens.add(p.lower())

    user_mentioned = False
    mention_context = ""
    if name_tokens:
        # Search in new content only (above first "From:" or "-----Original" marker)
        new_body = body
        for marker in ["From:", "-----Original Message", "________________________________"]:
            idx = body.find(marker)
            if idx > 0:
                new_body = body[:idx]
                break

        for token in name_tokens:
            pattern = rf'\b{re.escape(token)}\b'
            match = re.search(pattern, new_body, re.IGNORECASE)
            if match:
                user_mentioned = True
                start = max(0, match.start() - 40)
                end = min(len(new_body), match.end() + 60)
                mention_context = new_body[start:end].strip()
                break

    # --- Signal 3: Subject classification ---
    subject_lower = subject.lower().strip()
    if subject_lower.startswith("fw: fw:") or subject_lower.startswith("fwd: fwd:"):
        subject_type = "chain_forward"
    elif subject_lower.startswith("fw:") or subject_lower.startswith("fwd:"):
        subject_type = "forward"
    elif subject_lower.startswith("re:"):
        subject_type = "reply"
    else:
        subject_type = "new"

    # --- Signal 4: FYI / no-response language ---
    fyi_patterns = [
        r'\bfyi\b', r'\bfor your information\b', r'\bfor your reference\b',
        r'\bfor your records\b', r'\bjust a heads up\b', r'\blooping you in\b',
        r'\bkeeping you in the loop\b', r'\bfor visibility\b', r'\bfor awareness\b',
    ]
    terminal_patterns = [
        r'^thanks[!.]?\s*$', r'^thank you[!.]?\s*$', r'^got it[!.]?\s*$',
        r'^will do[!.]?\s*$', r'^sounds good[!.]?\s*$', r'^perfect[!.]?\s*$',
        r'^noted[!.]?\s*$', r'^acknowledged[!.]?\s*$', r'^received[!.]?\s*$',
    ]
    no_response_patterns = [
        r'\bno action needed\b', r'\bno response necessary\b',
        r'\bnothing needed from you\b', r'\bno reply needed\b',
    ]

    # Check new content only for FYI
    new_body_lower = (new_body if user_mentioned else body[:2000]).lower()
    fyi_detected = any(re.search(p, new_body_lower) for p in fyi_patterns)
    no_response_detected = any(re.search(p, new_body_lower) for p in no_response_patterns)

    # Terminal acknowledgments — short messages only
    body_stripped = body.strip()
    terminal = False
    if len(body_stripped) < 80:
        terminal = any(re.search(p, body_stripped.lower()) for p in terminal_patterns)

    # --- Signal 5: Intent classification ---
    if terminal:
        intent_category = "acknowledgment"
    elif fyi_detected or no_response_detected:
        intent_category = "informational"
    elif subject_type == "forward" or subject_type == "chain_forward":
        intent_category = "informational"
    elif re.search(r'\b(can you|could you|please|would you|need you to)\b', new_body_lower):
        intent_category = "direct_request"
        # Check if request is in new vs quoted content
    elif re.search(r'\b(update|status|progress|where are we)\b', new_body_lower):
        intent_category = "status_update"
    elif re.search(r'\b(schedule|meeting|call|calendar|available)\b', new_body_lower):
        intent_category = "scheduling"
    else:
        intent_category = "unclassified"

    return {
        "user_position": user_position,
        "to_count": to_count,
        "cc_count": cc_count,
        "total_recipients": total_recipients,
        "user_mentioned_by_name": user_mentioned,
        "name_mention_context": mention_context,
        "subject_type": subject_type,
        "fyi_language_detected": fyi_detected or no_response_detected,
        "terminal_acknowledgment": terminal,
        "intent_category": intent_category,
        "intent_in_new_content": True,
        # Signals that need external data — null/fallback states
        "thread_message_count": None,
        "user_replies_in_thread": 0,
        "user_active_in_thread": False,
        "sender_conditional_response_rate": None,
        "sender_emails_last_30d": None,
        "thread_velocity": None,
        "subsequent_replies_count": 0,
        "unique_subsequent_responders": 0,
    }


def supabase_row_to_email_data(row):
    """Convert a Supabase emails table row into the email_data dict
    that the pipeline classes expect.

    Args:
        row: dict from Supabase emails table.

    Returns:
        dict: email_data compatible with EmailFilter, ClaudeAnalyzer, etc.
    """
    return {
        "subject": row.get("subject", ""),
        "sender": row.get("sender", ""),
        "sender_email": row.get("sender_email", ""),
        "sender_name": row.get("sender_name", ""),
        "body": row.get("body", ""),
        "received_time": row.get("received_time", ""),
        "conversation_id": row.get("conversation_id"),
        "conversation_topic": row.get("conversation_topic"),
        "folder": row.get("folder", "Inbox"),
        "email_ref": row.get("email_ref", ""),
        "importance": _importance_to_int(row.get("importance", "Normal")),
        "has_attachments": row.get("has_attachments", False),
        "attachment_names": row.get("attachment_names", []),
        "to_field": row.get("to_field", ""),
        "cc_field": row.get("cc_field", ""),
        "flag_status": row.get("flag_status", "NotFlagged"),
        "recipients": row.get("recipients", []),
    }


def _importance_to_int(importance_str):
    """Convert OWA importance string to integer for pipeline compatibility."""
    mapping = {"Low": 0, "Normal": 1, "High": 2}
    return mapping.get(importance_str, 1)


def _parse_priority(priority_val):
    """Convert priority from Claude output to integer."""
    if isinstance(priority_val, int):
        return priority_val
    if priority_val == "x":
        return 1
    return 0


def _should_auto_skip(email_data, signals):
    """Return a skip reason string if signals alone can resolve this email.

    Conservative rules only — when in doubt, return None to send to Claude.
    Does NOT auto-skip based on user_position=CC alone.
    """
    if signals.get("terminal_acknowledgment") is True:
        return "terminal acknowledgment"

    sender = (email_data.get("sender", "") or "").lower()
    if "/o=exchangelabs/" in sender:
        return "automated Exchange system message"

    if (
        signals.get("fyi_language_detected") is True
        and signals.get("intent_category") in ("informational", "acknowledgment")
    ):
        return "FYI/no-response language detected"

    return None


def _is_recent(received_time, max_age_hours):
    """Check if an email is recent enough for draft generation."""
    if not received_time:
        return False
    try:
        received = datetime.fromisoformat(str(received_time).replace("Z", "+00:00"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        return received > cutoff
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Enriched pipeline helpers
# ---------------------------------------------------------------------------

def _fetch_batch_context(db, user_id, filtered_emails):
    """Fetch contacts, thread messages, and topic profile in batch.

    Returns:
        tuple: (contacts_map, threads_map, topic_profile)
    """
    # Collect unique sender emails
    sender_emails = list({
        (ed.get("sender_email") or ed.get("sender") or "").lower()
        for ed in filtered_emails
        if ed.get("sender_email") or ed.get("sender")
    })
    contacts_map = db.fetch_contacts_by_emails(user_id, sender_emails)

    # Collect unique conversation IDs
    conv_ids = list({
        ed["conversation_id"]
        for ed in filtered_emails
        if ed.get("conversation_id")
    })
    threads_map = db.fetch_thread_messages(user_id, conv_ids)

    topic_profile = db.fetch_user_topic_profile(user_id)

    return contacts_map, threads_map, topic_profile


def _build_thread_info(email_data, thread_row, contact, user_aliases):
    """Build thread_info dict for the scorer from DB thread data.

    Args:
        email_data: dict from supabase_row_to_email_data().
        thread_row: dict from conversations table (or None).
        contact: dict from contacts table (or None).
        user_aliases: list[str] of user email addresses.

    Returns:
        dict: thread_info for score_email().
    """
    info = {
        "total_messages": 1,
        "user_messages": 0,
        "participation_rate": None,
        "user_initiated": False,
        "hours_since_user_reply": None,
        "sender_events_count": None,
    }

    if contact:
        info["sender_events_count"] = contact.get("emails_per_month")

    if not thread_row:
        return info

    messages = thread_row.get("messages") or []
    if not messages:
        return info

    info["total_messages"] = len(messages)

    user_msgs = [
        m for m in messages
        if (m.get("sender_email") or "").lower() in user_aliases
    ]
    info["user_messages"] = len(user_msgs)
    info["participation_rate"] = len(user_msgs) / len(messages) if messages else None

    # User initiated?
    sorted_msgs = sorted(messages, key=lambda m: m.get("received_time") or "")
    if sorted_msgs:
        first_sender = (sorted_msgs[0].get("sender_email") or "").lower()
        info["user_initiated"] = first_sender in user_aliases

    # Hours since user's last reply
    if user_msgs:
        try:
            received = email_data.get("received_time")
            if received:
                inbound_dt = datetime.fromisoformat(
                    str(received).replace("Z", "+00:00")
                )
                if inbound_dt.tzinfo is None:
                    inbound_dt = inbound_dt.replace(tzinfo=timezone.utc)

                user_times = []
                for m in user_msgs:
                    t = m.get("received_time")
                    if t:
                        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        user_times.append(dt)

                if user_times:
                    last_reply = max(user_times)
                    hours = (inbound_dt - last_reply).total_seconds() / 3600
                    if hours >= 0:
                        info["hours_since_user_reply"] = hours
        except (ValueError, TypeError):
            pass

    return info


def _score_and_gate(db, filtered_emails, contacts_map, threads_map,
                    user_aliases, user_id):
    """Score each email and apply triage gates.

    Returns:
        tuple: (passing_emails, gated_count)
        Gated emails get auto-classified in the DB.
    """
    artifacts = _get_user_artifacts(db, user_id)
    passing = []
    gated_count = 0

    for ed in filtered_emails:
        sender = (ed.get("sender_email") or ed.get("sender") or "").lower()
        contact = contacts_map.get(sender)
        conv_id = ed.get("conversation_id")
        thread_row = threads_map.get(conv_id) if conv_id else None
        thread_info = _build_thread_info(ed, thread_row, contact, user_aliases)

        signals = ed.get("signals", {})
        raw_score, calibrated, tier, factors = score_email(
            ed, signals, contact, thread_info, artifacts
        )

        should_gate, gate_reason = check_triage_gate(
            calibrated, thread_info, artifacts
        )

        if should_gate:
            db_id = ed["_db_id"]
            db.insert_classification(db_id, user_id, {
                "needs_response": False,
                "action": "Auto-gated by scorer",
                "context": f"Gated: {gate_reason} (score={calibrated:.3f})",
            })
            db.update_email_status(db_id, "completed")
            gated_count += 1
            logger.info(
                f"  Gated ({gate_reason}): {ed.get('subject', '?')[:50]} "
                f"[{calibrated:.3f}]"
            )
            continue

        # Attach scoring data for enrichment
        ed["_raw_score"] = raw_score
        ed["_calibrated_prob"] = calibrated
        ed["_confidence_tier"] = tier
        ed["_factors"] = factors
        ed["_thread_info"] = thread_info
        passing.append(ed)

    return passing, gated_count


def _group_by_thread(emails):
    """Group emails by conversation_id. Singletons get their own group.

    Returns:
        list[list[dict]]: Groups ordered by earliest received_time.
    """
    groups = defaultdict(list)
    for ed in emails:
        key = ed.get("conversation_id") or f"_single_{id(ed)}"
        groups[key].append(ed)

    # Sort groups by earliest received_time
    sorted_groups = sorted(
        groups.values(),
        key=lambda g: min(
            (e.get("received_time") or "") for e in g
        ),
    )
    return sorted_groups


def _enrich_batch(emails, contacts_map, threads_map, user_aliases, profile):
    """Assemble enrichment records for a batch of emails.

    Returns:
        list[dict]: One enrichment record per email.
    """
    records = []
    for ed in emails:
        sender = (ed.get("sender_email") or ed.get("sender") or "").lower()
        contact = contacts_map.get(sender)
        conv_id = ed.get("conversation_id")
        thread_row = threads_map.get(conv_id) if conv_id else None
        thread_messages = (thread_row.get("messages") or []) if thread_row else []

        rec = assemble_enrichment(
            email_data=ed,
            signals=ed.get("signals", {}),
            raw_score=ed["_raw_score"],
            calibrated_prob=ed["_calibrated_prob"],
            confidence_tier=ed["_confidence_tier"],
            factors=ed["_factors"],
            contact=contact,
            thread_messages=thread_messages,
            user_aliases=user_aliases,
            profile=profile,
        )
        records.append(rec)
    return records


def _chunk_for_classification(thread_groups, max_per_call=8):
    """Pack thread groups into chunks of 5-10 emails for Haiku calls.

    Keeps same-thread emails together. Allows single-thread overflow.

    Returns:
        list[list[dict]]: Each inner list is a chunk of emails.
    """
    chunks = []
    current_chunk = []

    for group in thread_groups:
        # If adding this group would exceed max and we already have emails, start new chunk
        if current_chunk and len(current_chunk) + len(group) > max_per_call:
            chunks.append(current_chunk)
            current_chunk = []
        current_chunk.extend(group)

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def process_user_batch_enriched(db, user_id, profile, emails):
    """Process accumulated emails using the enriched scorer pipeline.

    Flow: filter → fetch context → score + gate → group by thread
         → enrich → batch-classify (Haiku) → draft (Sonnet)

    Returns:
        tuple: (emails_processed, drafts_generated)
    """
    from pipeline.api_client import submit_and_wait
    from pipeline.prompts import get_enriched_analysis_prompt

    config = build_config_from_profile(profile)
    user_aliases = [a.lower() for a in config.get("user_email_aliases", []) if a]
    run_id = db.create_pipeline_run(user_id, trigger_type="scheduled")
    batch_poll_interval = int(os.environ.get("BATCH_POLL_INTERVAL", "10"))
    batch_max_wait = int(os.environ.get("BATCH_MAX_WAIT", "900"))
    api_key = config.get("anthropic_api_key")

    try:
        # Step 1: Filter (unchanged)
        filtered = filter_emails(db, emails, user_id, config)
        if not filtered:
            logger.info(f"  User {user_id[:8]}...: all emails filtered out")
            db.update_pipeline_run(
                run_id, status="completed",
                emails_scanned=len(emails), emails_processed=0, drafts_generated=0,
            )
            return 0, 0

        # Step 2: Fetch batch context (3 DB queries)
        contacts_map, threads_map, topic_profile = _fetch_batch_context(
            db, user_id, filtered
        )
        logger.info(
            f"  Context: {len(contacts_map)} contacts, "
            f"{len(threads_map)} threads loaded"
        )

        # Step 3: Score + gate
        passing, gated_count = _score_and_gate(
            db, filtered, contacts_map, threads_map, user_aliases, user_id
        )
        if gated_count:
            logger.info(f"  Gated {gated_count} emails by scorer")
        if not passing:
            logger.info(f"  User {user_id[:8]}...: all emails gated")
            db.update_pipeline_run(
                run_id, status="completed",
                emails_scanned=len(emails),
                emails_processed=len(filtered),
                drafts_generated=0,
            )
            return len(filtered), 0

        # Step 4: Group by thread
        thread_groups = _group_by_thread(passing)

        # Step 5: Enrich
        enrichment_records = _enrich_batch(
            passing, contacts_map, threads_map, user_aliases, profile
        )
        logger.info(f"  Enriched {len(enrichment_records)} emails")

        # Attach enrichment data to email dicts for draft context
        email_to_enrichment = {rec["email_id"]: rec for rec in enrichment_records}
        for ed in passing:
            rec = email_to_enrichment.get(ed["_db_id"])
            if rec:
                ed["_enrichment"] = rec

        # Step 6: Chunk for classification
        chunks = _chunk_for_classification(thread_groups)

        # Step 7: Batch-classify via Batches API
        analyzer = ClaudeAnalyzer(config, system_prompt=get_enriched_analysis_prompt())
        batch_requests = []

        # Build one batch request per chunk
        for ci, chunk in enumerate(chunks):
            chunk_records = []
            for ed in chunk:
                db_id = ed["_db_id"]
                rec = email_to_enrichment.get(db_id)
                if rec:
                    chunk_records.append(rec)
            if chunk_records:
                req = analyzer.build_enriched_batch_params(
                    chunk_records, custom_id=f"classify_{ci}"
                )
                batch_requests.append(req)

        all_action_items = []

        if batch_requests:
            logger.info(
                f"  User {user_id[:8]}...: submitting {len(batch_requests)} "
                f"classification chunks ({len(passing)} emails)"
            )
            try:
                batch_results = submit_and_wait(
                    batch_requests,
                    api_key=api_key,
                    poll_interval=batch_poll_interval,
                    max_wait=batch_max_wait,
                )
            except Exception as e:
                logger.warning(f"  Batch classification failed: {e}")
                batch_results = {}

            # Parse results from each chunk
            for ci, chunk in enumerate(chunks):
                chunk_id = f"classify_{ci}"
                raw_text = batch_results.get(chunk_id)
                chunk_records = []
                for ed in chunk:
                    rec = email_to_enrichment.get(ed["_db_id"])
                    if rec:
                        chunk_records.append(rec)

                if raw_text and chunk_records:
                    items = analyzer.parse_enriched_batch_result(
                        raw_text, chunk_records
                    )
                    if items:
                        # Map _email_id back to email_data for process_classification_results
                        for item in items:
                            eid = item.get("_email_id")
                            if eid:
                                # Find the matching email's position in passing list
                                for pi, ed in enumerate(passing):
                                    if ed["_db_id"] == eid:
                                        item["email_index"] = pi + 1
                                        break
                        all_action_items.extend(items)

        # Fallback: sync classification for any emails without results
        classified_ids = {item.get("_email_id") for item in all_action_items}
        missing = [ed for ed in passing if ed["_db_id"] not in classified_ids]
        if missing:
            logger.info(f"  Sync fallback for {len(missing)} unclassified emails")
            fallback_items = analyzer.analyze_batch(missing)
            if fallback_items:
                all_action_items.extend(fallback_items)

        if not all_action_items:
            logger.warning(f"  User {user_id[:8]}...: classification returned no results")
            for ed in passing:
                db.update_email_status(ed["_db_id"], "completed")
            db.update_pipeline_run(
                run_id, status="completed",
                emails_scanned=len(emails),
                emails_processed=len(filtered),
                drafts_generated=0,
            )
            return len(filtered), 0

        # Step 8: Process results + draft generation (existing flow)
        draft_generator = DraftGenerator(
            config, system_prompt_template=get_draft_prompt_template()
        )
        emails_processed, draft_candidates = process_classification_results(
            db, all_action_items, passing, user_id, config, draft_generator
        )

        # Step 9: Draft generation via Batches API (unchanged from main.py)
        drafts_generated = 0
        if draft_candidates:
            draft_requests = []
            for candidate in draft_candidates:
                req = draft_generator.build_batch_params(
                    candidate["email_data"],
                    candidate["action_context"],
                    custom_id=candidate["db_id"],
                )
                draft_requests.append(req)

            logger.info(
                f"  User {user_id[:8]}...: submitting draft batch "
                f"({len(draft_requests)} drafts)"
            )

            try:
                draft_results = submit_and_wait(
                    draft_requests,
                    api_key=api_key,
                    poll_interval=batch_poll_interval,
                    max_wait=batch_max_wait,
                )
            except Exception as e:
                logger.warning(f"  Batch draft generation failed: {e}")
                draft_results = {}

            for candidate in draft_candidates:
                db_id = candidate["db_id"]
                draft_body = draft_results.get(db_id)

                if not draft_body:
                    draft_body = draft_generator.generate_draft(
                        candidate["email_data"], candidate["action_context"]
                    )

                if draft_body and draft_generator._validate_output(
                    draft_body, candidate["email_data"]
                ):
                    db.insert_draft(db_id, user_id, draft_body)
                    drafts_generated += 1
                    logger.info(
                        f"  Draft generated for: "
                        f"{candidate['email_data'].get('subject', '?')[:60]}"
                    )

        db.update_pipeline_run(
            run_id, status="completed",
            emails_scanned=len(emails),
            emails_processed=emails_processed + gated_count,
            drafts_generated=drafts_generated,
        )
        return emails_processed + gated_count, drafts_generated

    except Exception as e:
        logger.exception(f"  User {user_id[:8]}...: enriched pipeline error: {e}")
        for email in emails:
            try:
                db.update_email_status(email["id"], "error")
            except Exception:
                pass
        db.update_pipeline_run(
            run_id, status="failed", error_message=str(e)[:500]
        )
        return 0, 0
