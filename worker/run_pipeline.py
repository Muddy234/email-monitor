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

from pipeline.filter import EmailFilter
from pipeline.analyzer import ClaudeAnalyzer
from pipeline.drafts import DraftGenerator
from pipeline.prompts import get_analysis_prompt, get_draft_prompt_template

logger = logging.getLogger("worker")


def build_config_from_profile(profile):
    """Convert a Supabase profiles row into the config dict that
    EmailFilter, ClaudeAnalyzer, and DraftGenerator expect.

    The existing pipeline classes read config keys like
    'filter_blacklist_senders', 'claude_backend', etc.
    Profile rows store per-user settings; env vars fill in API keys.

    Args:
        profile: dict from profiles table.

    Returns:
        dict: Config dict compatible with existing pipeline classes.
    """
    import os

    config = {
        # Provider settings (not used by worker, but kept for compatibility)
        "email_provider": "supabase",
        "process_flagged_only": profile.get("process_flagged_only", True),
        "max_emails_to_scan": profile.get("max_emails_to_scan", 500),

        # User identity
        "user_email_aliases": profile.get("user_email_aliases", []),

        # Claude settings — from env vars (not stored in user profile)
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "claude_backend": "api",
        "classification_model": os.environ.get("CLASSIFICATION_MODEL", "haiku"),
        "draft_model": os.environ.get("DRAFT_MODEL", "sonnet"),
        "claude_cli_timeout_seconds": int(os.environ.get("CLAUDE_TIMEOUT", "120")),
        "max_body_chars": int(os.environ.get("MAX_BODY_CHARS", "8000")),
        "enable_response_signals": True,

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
    for email in emails:
        email_data = supabase_row_to_email_data(email)
        email_data["signals"] = build_signals(email_data, user_aliases)
        classification = email_filter.classify(email_data)

        if classification == "skip":
            logger.info(f"  Skipped (filter): {email_data.get('subject', '?')[:60]}")
            db_client.update_email_status(email["id"], "completed")
            db_client.insert_classification(
                email["id"], user_id,
                {"needs_response": False, "action": "skip", "context": "Filtered out by rules"},
            )
            continue

        skip_reason = _should_auto_skip(email_data, email_data["signals"])
        if skip_reason:
            logger.info(f"  Auto-skipped ({skip_reason}): {email_data.get('subject', '?')[:60]}")
            db_client.update_email_status(email["id"], "completed")
            db_client.insert_classification(
                email["id"], user_id,
                {
                    "needs_response": False,
                    "action": "Auto-skipped by signal rules",
                    "context": f"Skipped: {skip_reason}",
                },
            )
            continue

        email_data["_filter_result"] = classification
        email_data["_db_id"] = email["id"]
        filtered.append(email_data)

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

        db_client.insert_classification(db_id, user_id, {
            "needs_response": needs_response,
            "action": item.get("action", ""),
            "context": item.get("context", ""),
            "project": item.get("project", ""),
            "priority": _parse_priority(item.get("priority", "")),
        })

        email_age_ok = _is_recent(email_data.get("received_time"), draft_max_age_hours)
        if not email_age_ok and needs_response:
            logger.info(f"  Skipping draft (too old): {email_data.get('subject', '?')[:60]}")

        if needs_response and email_age_ok and config.get("enable_draft_generation", True):
            action_context = {
                "action": item.get("action", ""),
                "context": item.get("context", ""),
            }
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


def process_email_batch(db_client, emails, user_id, config):
    """Process a batch of claimed emails through the pipeline (sync path).

    This is the original synchronous flow — filter → classify → draft.
    The batch-based main loop uses filter_emails / process_classification_results
    directly for async batching.
    """
    analyzer = ClaudeAnalyzer(config, system_prompt=get_analysis_prompt())
    draft_generator = DraftGenerator(
        config,
        system_prompt_template=get_draft_prompt_template(),
    )

    # Step 1: Filter
    filtered_emails = filter_emails(db_client, emails, user_id, config)
    if not filtered_emails:
        logger.info("  No emails passed filter")
        return {"emails_processed": 0, "drafts_generated": 0}

    # Step 2: Claude analysis
    logger.info(f"  Analyzing {len(filtered_emails)} emails with Claude...")
    action_items = analyzer.analyze_batch(filtered_emails)

    if not action_items:
        logger.warning("  Claude analysis returned no results")
        for ed in filtered_emails:
            db_client.update_email_status(ed["_db_id"], "completed")
        return {"emails_processed": len(filtered_emails), "drafts_generated": 0}

    # Step 3: Classify + collect draft candidates
    emails_processed, draft_candidates = process_classification_results(
        db_client, action_items, filtered_emails, user_id, config, draft_generator
    )

    # Step 4: Generate drafts (sync, one at a time)
    drafts_generated = 0
    for candidate in draft_candidates:
        draft_body = draft_generator.generate_draft(
            candidate["email_data"], candidate["action_context"]
        )
        if draft_body:
            db_client.insert_draft(candidate["db_id"], user_id, draft_body)
            drafts_generated += 1
            logger.info(f"  Draft generated for: {candidate['email_data'].get('subject', '?')[:60]}")

    return {"emails_processed": emails_processed, "drafts_generated": drafts_generated}


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
