"""Pipeline orchestration for the Railway worker.

Ports the core logic from pipeline/runner.py, adapted for Supabase I/O:
- Reads email data from Supabase rows (not from a provider).
- Writes results to Supabase tables (not to Outlook via provider).
- Reuses existing EmailFilter, ClaudeAnalyzer, DraftGenerator classes.
"""

import logging

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
        "claude_cli_model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
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

        # Filter settings — defaults (could be moved to profile later)
        "filter_blacklist_senders": "",
        "filter_blacklist_subject_patterns": "",
        "filter_whitelist_senders": "",
        "filter_whitelist_domains": "",
        "filter_project_keywords": "",
        "filter_auto_important_patterns": "",
        "filter_direct_recipient": "",
    }

    return config


def process_email_batch(db_client, emails, user_id, config):
    """Process a batch of claimed emails through the pipeline.

    Args:
        db_client: SupabaseWorkerClient instance.
        emails: list[dict] — claimed email rows from Supabase.
        user_id: UUID string.
        config: dict — built from build_config_from_profile().

    Returns:
        dict: Summary with emails_processed, drafts_generated counts.
    """
    email_filter = EmailFilter(config)
    analyzer = ClaudeAnalyzer(config, system_prompt=get_analysis_prompt())
    draft_generator = DraftGenerator(
        config,
        system_prompt_template=get_draft_prompt_template(),
    )

    emails_processed = 0
    drafts_generated = 0

    # Step 1: Filter emails
    filtered_emails = []
    for email in emails:
        email_data = supabase_row_to_email_data(email)
        classification = email_filter.classify(email_data)

        if classification == "skip":
            logger.info(f"  Skipped: {email_data.get('subject', '?')[:60]}")
            db_client.update_email_status(email["id"], "completed")
            db_client.insert_classification(
                email["id"], user_id,
                {"needs_response": False, "action": "skip", "context": "Filtered out by rules"},
            )
            continue

        email_data["_filter_result"] = classification
        email_data["_db_id"] = email["id"]
        filtered_emails.append(email_data)

    if not filtered_emails:
        logger.info("  No emails passed filter")
        return {"emails_processed": 0, "drafts_generated": 0}

    # Step 2: Claude analysis (batch)
    logger.info(f"  Analyzing {len(filtered_emails)} emails with Claude...")
    action_items = analyzer.analyze_batch(filtered_emails)

    if not action_items:
        logger.warning("  Claude analysis returned no results")
        for ed in filtered_emails:
            db_client.update_email_status(ed["_db_id"], "completed")
        return {"emails_processed": len(filtered_emails), "drafts_generated": 0}

    # Step 3: Process action items — classify + generate drafts
    # Build index map: email_index (1-based) → email_data
    email_index_map = {i + 1: ed for i, ed in enumerate(filtered_emails)}

    for item in action_items:
        email_idx = item.get("email_index", 0)
        email_data = email_index_map.get(email_idx)
        if not email_data:
            continue

        db_id = email_data["_db_id"]
        needs_response = item.get("needs_response", False)

        # Insert classification
        db_client.insert_classification(db_id, user_id, {
            "needs_response": needs_response,
            "action": item.get("action", ""),
            "context": item.get("context", ""),
            "project": item.get("project", ""),
            "priority": _parse_priority(item.get("priority", "")),
        })

        # Generate draft if needed
        if needs_response and config.get("enable_draft_generation", True):
            action_context = {
                "action": item.get("action", ""),
                "context": item.get("context", ""),
            }

            # Fetch conversation context if available
            conv_id = email_data.get("conversation_id")
            if conv_id:
                conv_messages = db_client.fetch_conversation_context(user_id, conv_id)
                if conv_messages:
                    action_context["conversation_history"] = conv_messages

            draft_body = draft_generator.generate_draft(email_data, action_context)
            if draft_body:
                db_client.insert_draft(db_id, user_id, draft_body)
                drafts_generated += 1
                logger.info(f"  Draft generated for: {email_data.get('subject', '?')[:60]}")

        db_client.update_email_status(db_id, "completed")
        emails_processed += 1

    # Mark any remaining filtered emails that didn't get an action item
    for ed in filtered_emails:
        db_id = ed["_db_id"]
        # If not already completed above, mark completed
        try:
            db_client.update_email_status(db_id, "completed")
        except Exception:
            pass

    return {"emails_processed": emails_processed, "drafts_generated": drafts_generated}


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
