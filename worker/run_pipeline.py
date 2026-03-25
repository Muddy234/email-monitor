"""Pipeline orchestration for the Railway worker.

Ports the core logic for Supabase I/O:
- Reads email data from Supabase rows.
- Writes results to Supabase tables.
- Uses EmailFilter, signal_extractor, and DraftGenerator.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

from pipeline.filter import EmailFilter
from pipeline.drafts import DraftGenerator
from pipeline.prompts import (
    get_draft_prompt_template, build_notable_summary_prompt,
    NOTABLE_SUMMARY_SYSTEM_PROMPT,
)
from pipeline.signal_extractor import (
    extract_signals, extract_signals_batch_params, parse_signal_response,
)
from pipeline.pre_process import (
    pre_process_email, resolve_sender_tier, compute_thread_meta,
)

logger = logging.getLogger("worker")


def build_config_from_profile(profile):
    """Convert a Supabase profiles row into the config dict that
    EmailFilter and DraftGenerator expect.

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

        # Draft settings — display_name from profile, fall back to env vars
        "draft_user_name": profile.get("display_name") or os.environ.get("DRAFT_USER_NAME", ""),
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
        "filter_auto_important_patterns": [],
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
        classification, filter_reason = email_filter.classify(email_data)

        if classification == "skip":
            logger.info(f"  Skipped ({filter_reason}): {email_data.get('subject', '?')[:60]}")
            skip_ids.append(email["id"])
            skip_classifications.append({
                "email_id": email["id"],
                "user_id": user_id,
                "needs_response": False,
                "action": "skip",
                "context": f"Filter: {filter_reason}",
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
        db_client.bulk_update_email_status(skip_ids, "processed")
        db_client.bulk_insert_classifications(skip_classifications)
        logger.info(f"  Batch-wrote {len(skip_ids)} skipped emails")

    return filtered


def process_classification_results(db_client, action_items, filtered_emails,
                                   user_id, config, draft_generator,
                                   style_guide="", behavioral_profile="",
                                   contacts_map=None):
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

            if style_guide:
                action_context["style_guide"] = style_guide
            if behavioral_profile:
                action_context["behavioral_profile"] = behavioral_profile

            conv_id = email_data.get("conversation_id")
            if conv_id:
                thread_emails = db_client.fetch_thread_emails(
                    user_id, conv_id,
                    before_time=email_data.get("received_time"),
                )
                if thread_emails:
                    action_context["thread_emails"] = thread_emails

            # Attach contact for draft tone/context
            if contacts_map:
                sender = (email_data.get("sender_email") or email_data.get("sender") or "").lower()
                contact = contacts_map.get(sender)
                if contact:
                    email_data["sender_contact"] = contact

            draft_candidates.append({
                "db_id": db_id,
                "email_data": email_data,
                "action_context": action_context,
            })

        db_client.update_email_status(db_id, "processed")
        emails_processed += 1

    # Mark any remaining filtered emails that didn't get an action item
    for ed in filtered_emails:
        try:
            db_client.update_email_status(ed["_db_id"], "processed")
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
        dict: email_data compatible with EmailFilter, signal_extractor, etc.
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
    threads_map = db.fetch_thread_stats(user_id, conv_ids)

    topic_profile = db.fetch_user_topic_profile(user_id)

    return contacts_map, threads_map, topic_profile


def _fetch_thread_emails_batch(db, user_id, filtered_emails):
    """Fetch prior thread emails for all conversations in a single query.

    Returns:
        dict: conversation_id → list[dict] of prior email rows.
    """
    conv_ids = list({
        ed["conversation_id"]
        for ed in filtered_emails
        if ed.get("conversation_id")
    })
    if not conv_ids:
        return {}

    try:
        result = (
            db.client.table("emails")
            .select("id, conversation_id, sender, sender_name, body, received_time, subject")
            .eq("user_id", user_id)
            .in_("conversation_id", conv_ids)
            .order("received_time", desc=True)
            .limit(1000)
            .execute()
        )
    except Exception as e:
        logger.warning(f"  Failed to bulk-fetch thread emails: {e}")
        return {}

    thread_emails_map = {}
    for row in (result.data or []):
        cid = row["conversation_id"]
        thread_emails_map.setdefault(cid, []).append(row)

    # Limit to 10 per conversation (matching original behavior)
    for cid in thread_emails_map:
        thread_emails_map[cid] = thread_emails_map[cid][:10]

    return thread_emails_map


def _build_thread_info(email_data, thread_row, contact, user_aliases):
    """Build thread_info dict from DB thread data.

    Args:
        email_data: dict from supabase_row_to_email_data().
        thread_row: dict from threads table (aggregate stats) or None.
        contact: dict from contacts table (or None).
        user_aliases: list[str] of user email addresses.

    Returns:
        dict: thread_info with participation stats.
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
        info["sender_events_count"] = contact.get("total_received")

    if not thread_row:
        conv_id = email_data.get("conversation_id")
        if conv_id:
            logger.debug(f"No thread stats for conversation_id={conv_id}")
        return info

    info["total_messages"] = thread_row.get("total_messages", 1)
    info["user_messages"] = thread_row.get("user_messages", 0)
    info["participation_rate"] = thread_row.get("participation_rate")
    info["user_initiated"] = thread_row.get("user_initiated", False)

    return info


def _update_response_labels(db, user_id, threads_map, user_aliases):
    """Retroactively label response_events as responded=True.

    Scans thread messages for user-sent replies. Any inbound message
    preceding a user reply in the same thread gets labeled responded=True.
    This is retrospective — only affects future retraining, not live scores.
    """
    responded_ids = []
    for conv_id, thread_row in threads_map.items():
        messages = thread_row.get("messages") or []
        if len(messages) < 2:
            continue

        sorted_msgs = sorted(messages, key=lambda m: m.get("received_time") or "")
        for i, msg in enumerate(sorted_msgs):
            sender = (msg.get("sender_email") or "").lower()
            if sender in user_aliases:
                # All preceding inbound messages in this thread were "responded to"
                for prev in sorted_msgs[:i]:
                    prev_sender = (prev.get("sender_email") or "").lower()
                    if prev_sender not in user_aliases and prev.get("id"):
                        responded_ids.append(prev["id"])

    if responded_ids:
        unique_ids = list(set(responded_ids))
        try:
            db.label_response_events_responded(user_id, unique_ids)
            logger.info(f"  Labeled {len(unique_ids)} response_events as responded")
        except Exception as e:
            logger.warning(f"  Failed to label response_events: {e}")


def _update_contact_stats(db, user_id, filtered_emails, contacts_map,
                          user_domain=""):
    """Create/update contact records for senders not yet in contacts table."""
    seen = {}
    for ed in filtered_emails:
        sender = (ed.get("sender_email") or ed.get("sender") or "").lower()
        if sender and sender not in contacts_map:
            rt = ed.get("received_time")
            if sender not in seen or (rt and rt > seen[sender]["received_time"]):
                sender_name = ed.get("sender_name") or ""
                # Infer contact_type from domain match
                domain = sender.split("@")[1] if "@" in sender else ""
                contact_type = (
                    "internal_colleague"
                    if domain and user_domain and domain == user_domain
                    else "unknown"
                )
                seen[sender] = {
                    "email": sender,
                    "received_time": rt,
                    "name": sender_name or None,
                    "contact_type": contact_type,
                }

    new_senders = list(seen.values())
    if new_senders:
        try:
            db.bulk_upsert_contact_stats(user_id, new_senders)
            logger.info(f"  Upserted {len(new_senders)} new contact stats")
        except Exception as e:
            logger.warning(f"  Failed to upsert contact stats: {e}")



# ---------------------------------------------------------------------------
# Domain tiers cache (module-level, refreshed per worker cycle)
# ---------------------------------------------------------------------------

_domain_tiers_cache = {}   # {user_id: {domain: tier}}
_domain_tiers_ts = {}      # {user_id: loaded_at}
_DOMAIN_TIERS_TTL = 300    # 5 min


def _get_domain_tiers(db, user_id):
    """Load domain tiers for a user with 5-min cache."""
    import time
    global _domain_tiers_cache, _domain_tiers_ts
    now = time.time()
    cached = _domain_tiers_cache.get(user_id)
    if cached and (now - _domain_tiers_ts.get(user_id, 0)) < _DOMAIN_TIERS_TTL:
        return cached
    tiers = db.fetch_domain_tiers(user_id)
    _domain_tiers_cache[user_id] = tiers
    _domain_tiers_ts[user_id] = now
    return tiers


def _resolve_user_domain(profile):
    """Extract the user's org domain from their email aliases or profile email."""
    aliases = profile.get("user_email_aliases") or []
    for alias in aliases:
        if "@" in alias:
            return alias.split("@")[1].lower()
    # Fall back to profile email
    email = profile.get("email", "")
    if "@" in email:
        return email.split("@")[1].lower()
    return ""


# ---------------------------------------------------------------------------
# Per-contact overrides (post-signal)
# ---------------------------------------------------------------------------

def _apply_contact_overrides(signals, contact):
    """Apply per-contact overrides to signal extraction output.

    Enforces is_vip, priority_override, and draft_preference from the
    contacts table after Haiku's signal extraction has run.
    """
    if not contact:
        return signals

    if contact.get("is_vip"):
        signals["pri"] = "high"

    prio = contact.get("priority_override")
    if prio in ("high", "med", "low"):
        signals["pri"] = prio

    draft_pref = contact.get("draft_preference")
    if draft_pref == "always":
        signals["draft"] = True
    elif draft_pref == "never":
        signals["draft"] = False

    return signals


# ---------------------------------------------------------------------------
# Signal extraction pipeline (replaces scorer + classify)
# ---------------------------------------------------------------------------

def process_user_batch_signals(db, user_id, profile, emails):
    """Process emails using the signal extraction pipeline.

    Flow: filter → pre-process + context fetch → signal extraction (Haiku)
         → draft (Sonnet)

    This replaces process_user_batch_enriched. The old scorer.py, enrichment.py,
    and Haiku classify call are replaced by a single Haiku signal extraction call.

    Returns:
        tuple: (emails_processed, drafts_generated)
    """
    from pipeline.api_client import submit_and_wait

    config = build_config_from_profile(profile)
    user_aliases = [a.lower() for a in config.get("user_email_aliases", []) if a]
    run_id = db.create_pipeline_run(user_id, trigger_type="scheduled")
    batch_poll_interval = int(os.environ.get("BATCH_POLL_INTERVAL", "3"))
    batch_max_wait = int(os.environ.get("BATCH_MAX_WAIT", "900"))
    api_key = config.get("anthropic_api_key")
    draft_max_age_hours = int(os.environ.get("DRAFT_MAX_AGE_HOURS", "24"))

    try:
        # ── Stage 1: Filter ──────────────────────────────────────
        filtered = filter_emails(db, emails, user_id, config)
        if not filtered:
            logger.info(f"  User {user_id[:8]}...: all emails filtered out")
            db.update_pipeline_run(
                run_id, status="completed",
                emails_scanned=len(emails), emails_processed=0, drafts_generated=0,
            )
            return 0, 0

        # ── Stage 2: Pre-process + context fetch ─────────────────
        # Fetch batch context (contacts, threads)
        contacts_map, threads_map, _ = _fetch_batch_context(db, user_id, filtered)
        thread_emails_map = _fetch_thread_emails_batch(db, user_id, filtered)
        domain_tiers = _get_domain_tiers(db, user_id)
        user_domain = _resolve_user_domain(profile)

        # Upsert contact stats for unknown senders
        _update_contact_stats(db, user_id, filtered, contacts_map, user_domain)

        # Retroactive response labeling
        _update_response_labels(db, user_id, threads_map, user_aliases)

        logger.info(
            f"  Context: {len(contacts_map)} contacts, "
            f"{len(threads_map)} threads, {len(domain_tiers)} domain tiers"
        )

        # ── Stage 3: Signal extraction (Haiku batch) ─────────────
        batch_requests = []
        email_context = {}  # email_id → context needed for post-processing

        # Resolve user identity for action-target detection
        user_name = config.get("draft_user_name") or ""
        user_email_primary = user_aliases[0] if user_aliases else ""

        for ed in filtered:
            db_id = ed["_db_id"]
            sender_email = (ed.get("sender_email") or ed.get("sender") or "").lower()
            contact = contacts_map.get(sender_email)
            conv_id = ed.get("conversation_id")
            thread_row = threads_map.get(conv_id) if conv_id else None

            # Pre-process body (use prior thread emails for content-aware isolation)
            thread_emails = thread_emails_map.get(conv_id, []) if conv_id else []
            prior_bodies = [te["body"] for te in thread_emails if te.get("body")]
            clean_body = pre_process_email(ed, prior_bodies=prior_bodies)

            # Resolve sender tier
            sender_tier = resolve_sender_tier(
                sender_email, contact, user_domain, domain_tiers
            )

            # Thread metadata
            thread_depth, has_unanswered = compute_thread_meta(
                thread_row, sender_email, user_aliases,
                thread_emails=thread_emails,
            )

            # Compute user position (TO vs CC) for this email
            to_raw = (ed.get("to_field") or "").lower()
            cc_raw = (ed.get("cc_field") or "").lower()
            user_position = "UNKNOWN"
            for alias in user_aliases:
                if alias in to_raw:
                    user_position = "TO"
                    break
                if alias in cc_raw:
                    user_position = "CC"
                    break

            # Build batch request
            req = extract_signals_batch_params(
                email_body=clean_body,
                subject=ed.get("subject", ""),
                sender_name=ed.get("sender_name") or sender_email.split("@")[0],
                sender_email=sender_email,
                sender_tier=sender_tier,
                thread_depth=thread_depth,
                has_unanswered=has_unanswered,
                custom_id=db_id,
                user_name=user_name,
                user_email=user_email_primary,
                user_position=user_position,
                to_field=ed.get("to_field") or "",
                cc_field=ed.get("cc_field") or "",
                contact_type=contact.get("contact_type", "") if contact else "",
                significance=contact.get("relationship_significance", "") if contact else "",
            )
            batch_requests.append(req)

            # Stash context for post-processing
            email_context[db_id] = {
                "ed": ed,
                "sender_email": sender_email,
                "sender_tier": sender_tier,
                "contact": contact,
                "conv_id": conv_id,
                "thread_depth": thread_depth,
                "user_position": user_position,
            }

        # Submit batch
        batch_results = {}
        if batch_requests:
            logger.info(
                f"  User {user_id[:8]}...: submitting signal extraction batch "
                f"({len(batch_requests)} emails)"
            )
            try:
                batch_results, signal_usage = submit_and_wait(
                    batch_requests,
                    api_key=api_key,
                    poll_interval=batch_poll_interval,
                    max_wait=batch_max_wait,
                )
                if signal_usage:
                    db.record_token_usage(user_id, "haiku", "signals", signal_usage)
            except Exception as e:
                logger.warning(f"  Signal extraction batch failed: {e}")

        # Fallback: sync extraction for emails without batch results
        for db_id, ctx in email_context.items():
            if db_id not in batch_results or batch_results[db_id] is None:
                ed = ctx["ed"]
                clean_body = pre_process_email(ed)
                try:
                    signals, fallback_usage = extract_signals(
                        email_body=clean_body,
                        subject=ed.get("subject", ""),
                        sender_name=ed.get("sender_name") or ctx["sender_email"].split("@")[0],
                        sender_email=ctx["sender_email"],
                        sender_tier=ctx["sender_tier"],
                        thread_depth=ctx["thread_depth"],
                        has_unanswered=False,
                        api_key=api_key,
                    )
                    if fallback_usage:
                        db.record_token_usage(user_id, "haiku", "signals", fallback_usage)
                    # Serialize back to raw JSON for uniform post-processing
                    import json
                    batch_results[db_id] = json.dumps(signals)
                except Exception as e:
                    logger.warning(f"  Sync signal extraction failed for {db_id[:8]}: {e}")

        # ── Stage 4: Post-process results ────────────────────────
        response_events = []
        draft_candidates = []
        notable_candidates = []
        emails_processed = 0

        for db_id, ctx in email_context.items():
            ed = ctx["ed"]
            raw_text = batch_results.get(db_id)
            signals = parse_signal_response(raw_text)
            contact = ctx["contact"]
            signals = _apply_contact_overrides(signals, contact)

            # Build response_event for persistence
            event = {
                "email_id": db_id,
                "sender_email": ctx["sender_email"],
                "received_time": ed.get("received_time"),
                "conversation_id": ctx["conv_id"],
                "subject": ed.get("subject", ""),
                "thread_depth": ctx["thread_depth"],
                "sender_tier": ctx["sender_tier"],
                # Signal fields
                "mc": signals["mc"],
                "ar": signals["ar"],
                "ub": signals["ub"],
                "dl": signals["dl"],
                "rt": signals["rt"],
                "target": signals.get("target", "user"),
                "pri": signals["pri"],
                "draft": signals["draft"],
                "reason": signals["reason"],
                # Legacy fields (backfill for transition)
                "user_position": ctx.get("user_position"),
                "total_recipients": ed.get("signals", {}).get("total_recipients"),
                "has_question": signals["ar"],  # approximate mapping
                "has_action_language": signals["ar"],
                "sender_is_internal": ctx["sender_tier"] == "I",
            }
            response_events.append(event)

            # Write classification (backfill old columns for transition)
            classification = {
                "needs_response": signals["draft"],
                "action": signals["reason"],
                "context": signals["reason"],
                "project": "",
                "priority": {"high": 2, "med": 1, "low": 0}.get(signals["pri"], 0),
            }
            db.insert_classification(db_id, user_id, classification)
            db.update_email_status(db_id, "processed")
            emails_processed += 1

            # Check if draft needed
            if signals["draft"]:
                email_age_ok = _is_recent(ed.get("received_time"), draft_max_age_hours)
                if email_age_ok and config.get("enable_draft_generation", True):
                    action_context = {
                        "reason": signals["reason"],
                        "action": signals["reason"],
                        "context": signals["reason"],
                        "user_aliases": user_aliases,
                    }

                    # Add thread emails for thread context
                    te = thread_emails_map.get(conv_id, []) if conv_id else []
                    if te:
                        action_context["thread_emails"] = te

                    style_guide = profile.get("writing_style_guide") or ""
                    if style_guide:
                        action_context["style_guide"] = style_guide

                    behavioral_profile = profile.get("behavioral_profile") or ""
                    if behavioral_profile:
                        action_context["behavioral_profile"] = behavioral_profile

                    # Attach contact for draft tone/context
                    if contact:
                        ed["sender_contact"] = contact

                    draft_candidates.append({
                        "db_id": db_id,
                        "email_data": ed,
                        "action_context": action_context,
                    })
                elif not email_age_ok:
                    logger.info(f"  Skipping draft (too old): {ed.get('subject', '?')[:60]}")
            else:
                # Not draft-worthy — check if notable
                is_notable = (
                    signals["pri"] in ("high", "med")
                    or signals["mc"] is True
                    or ctx["sender_tier"] in ("C", "I")
                    or signals["rt"] != "none"
                )
                if is_notable:
                    notable_candidates.append({
                        "db_id": db_id,
                        "email_data": ed,
                        "conv_id": ed.get("conversation_id"),
                    })

        # Persist response events
        if response_events:
            try:
                db.upsert_response_events(user_id, response_events)
            except Exception as e:
                logger.warning(f"  Failed to persist response_events: {e}")

        # ── Stage 4b: Notable summaries (Haiku batch) ────────────
        if notable_candidates:
            from pipeline.api_client import resolve_model as _resolve_model

            notable_requests = []
            for candidate in notable_candidates:
                ed = candidate["email_data"]
                conv_id = candidate["conv_id"]
                conv_history = thread_emails_map.get(conv_id, []) if conv_id else None
                conv_history = conv_history or None

                user_msg = build_notable_summary_prompt(ed, conv_history)
                notable_requests.append({
                    "custom_id": candidate["db_id"],
                    "params": {
                        "model": _resolve_model("haiku"),
                        "max_tokens": 500,
                        "temperature": 0,
                        "system": [{
                            "type": "text",
                            "text": NOTABLE_SUMMARY_SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        "messages": [{"role": "user", "content": user_msg}],
                    },
                })

            logger.info(
                f"  User {user_id[:8]}...: submitting notable summary batch "
                f"({len(notable_requests)} emails)"
            )

            try:
                notable_results, notable_usage = submit_and_wait(
                    notable_requests,
                    api_key=api_key,
                    poll_interval=batch_poll_interval,
                    max_wait=batch_max_wait,
                )
                if notable_usage:
                    db.record_token_usage(user_id, "haiku", "summary", notable_usage)
            except Exception as e:
                logger.warning(f"  Notable summary batch failed: {e}")
                notable_results = {}

            # Store summaries via targeted updates
            for candidate in notable_candidates:
                db_id = candidate["db_id"]
                summary = notable_results.get(db_id)
                if summary and summary.strip():
                    try:
                        db.client.table("response_events").update(
                            {"summary": summary.strip()}
                        ).eq("email_id", db_id).eq("user_id", user_id).execute()
                    except Exception as e:
                        logger.warning(f"  Failed to store notable summary for {db_id[:8]}: {e}")

        # ── Stage 5: Draft generation (Sonnet batch) ─────────────
        drafts_generated = 0
        if draft_candidates:
            draft_generator = DraftGenerator(
                config, system_prompt_template=get_draft_prompt_template()
            )

            # Deduplicate by db_id
            seen_ids = set()
            unique_candidates = []
            for candidate in draft_candidates:
                if candidate["db_id"] not in seen_ids:
                    seen_ids.add(candidate["db_id"])
                    unique_candidates.append(candidate)
            draft_candidates = unique_candidates

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
                draft_results, draft_usage = submit_and_wait(
                    draft_requests,
                    api_key=api_key,
                    poll_interval=batch_poll_interval,
                    max_wait=batch_max_wait,
                )
                if draft_usage:
                    db.record_token_usage(user_id, "sonnet", "draft", draft_usage)
            except Exception as e:
                logger.warning(f"  Batch draft generation failed: {e}")
                draft_results = {}

            for candidate in draft_candidates:
                db_id = candidate["db_id"]
                draft_body = draft_results.get(db_id)

                if not draft_body:
                    cleaned, fallback_usage, thinking = draft_generator.generate_draft(
                        candidate["email_data"], candidate["action_context"]
                    )
                    if fallback_usage:
                        db.record_token_usage(user_id, "sonnet", "draft", fallback_usage)
                else:
                    # Batch path: extract thinking before validation strips it
                    thinking = DraftGenerator._extract_thinking(draft_body)
                    cleaned = draft_generator._validate_output(
                        draft_body, candidate["email_data"]
                    )
                if cleaned:
                    db.insert_draft(db_id, user_id, cleaned)
                    drafts_generated += 1
                    logger.info(
                        f"  Draft generated for: "
                        f"{candidate['email_data'].get('subject', '?')[:60]}"
                    )

                # Store thinking as summary on response_event
                if thinking:
                    try:
                        db.client.table("response_events").update(
                            {"summary": thinking}
                        ).eq("email_id", db_id).eq("user_id", user_id).execute()
                    except Exception as e:
                        logger.warning(f"  Failed to store draft thinking for {db_id[:8]}: {e}")

        db.update_pipeline_run(
            run_id, status="completed",
            emails_scanned=len(emails),
            emails_processed=emails_processed,
            drafts_generated=drafts_generated,
        )
        return emails_processed, drafts_generated

    except Exception as e:
        logger.exception(f"  User {user_id[:8]}...: signal pipeline error: {e}")
        for email in emails:
            try:
                db.update_email_status(email["id"], "error")
            except Exception:
                pass
        db.update_pipeline_run(
            run_id, status="failed", error_message=str(e)[:500]
        )
        return 0, 0
