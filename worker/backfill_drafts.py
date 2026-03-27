"""One-off script to backfill missing drafts for specific emails.

Usage (via Railway):
    railway run python worker/backfill_drafts.py

Or locally with env vars set:
    python worker/backfill_drafts.py

Finds emails with needs_response=true classification but no draft row,
generates drafts via Anthropic API, and inserts them.
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill")

# Add worker dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supabase_client import SupabaseWorkerClient
from run_pipeline import supabase_row_to_email_data, build_config_from_profile
from pipeline.drafts import DraftGenerator
from pipeline.prompts import get_draft_prompt_template


# Target email IDs — the 2 Round 3 emails that were dropped
TARGET_EMAIL_IDS = [
    "6abcbdbd-9d7f-4b7b-b096-45d2034eb2b0",  # Orion Health Systems
    "1e978bc9-8e66-4b9f-ab53-fd4ef2ba7716",  # Relocation Package Dispute
]


def backfill_drafts(db, email_ids=None):
    """Generate drafts for emails that have classification but no draft.

    Args:
        db: SupabaseWorkerClient instance.
        email_ids: Optional list of specific email IDs. If None, finds all
                   emails with needs_response=true and no draft row.
    """
    if email_ids:
        # Fetch specific emails
        result = (
            db.client.table("emails")
            .select("*, classifications!inner(needs_response, reason, priority)")
            .in_("id", email_ids)
            .execute()
        )
    else:
        # Find all emails missing drafts
        result = db.client.rpc("find_emails_missing_drafts").execute()
        if not result.data:
            logger.info("No emails missing drafts found")
            return

    emails = result.data or []
    if not emails:
        logger.info("No matching emails found")
        return

    logger.info(f"Found {len(emails)} emails to backfill")

    # Group by user_id
    by_user = {}
    for row in emails:
        uid = row["user_id"]
        if uid not in by_user:
            by_user[uid] = []
        by_user[uid].append(row)

    for user_id, user_emails in by_user.items():
        profile = db.fetch_user_config(user_id)
        if not profile:
            logger.warning(f"No profile for user {user_id[:8]}, skipping")
            continue

        config = build_config_from_profile(profile)
        draft_generator = DraftGenerator(
            config, system_prompt_template=get_draft_prompt_template()
        )

        # Get user aliases for thread context
        user_aliases = [
            a.lower() for a in (profile.get("user_email_aliases") or [])
        ]

        for row in user_emails:
            email_id = row["id"]
            subject = row.get("subject", "(no subject)")

            # Check if draft already exists (race condition guard)
            existing = (
                db.client.table("drafts")
                .select("id")
                .eq("email_id", email_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                logger.info(f"  Draft already exists for '{subject[:50]}', skipping")
                continue

            # Build email_data
            ed = supabase_row_to_email_data(row)
            ed["_db_id"] = email_id

            # Build action_context from classification
            classification = row.get("classifications", [{}])
            if isinstance(classification, list):
                classification = classification[0] if classification else {}
            reason = classification.get("reason", "")

            action_context = {
                "reason": reason,
                "action": reason,
                "context": reason,
                "user_aliases": user_aliases,
            }

            # Add thread emails if conversation exists
            conv_id = row.get("conversation_id")
            if conv_id:
                thread_result = (
                    db.client.table("emails")
                    .select("id, sender, sender_name, body, received_time")
                    .eq("conversation_id", conv_id)
                    .neq("id", email_id)
                    .order("received_time", desc=True)
                    .limit(5)
                    .execute()
                )
                if thread_result.data:
                    action_context["thread_emails"] = thread_result.data

            # Add style guide and behavioral profile
            style_guide = profile.get("writing_style_guide") or ""
            if style_guide:
                action_context["style_guide"] = style_guide

            behavioral_profile = profile.get("behavioral_profile") or ""
            if behavioral_profile:
                action_context["behavioral_profile"] = behavioral_profile

            # Generate draft
            logger.info(f"  Generating draft for: {subject[:60]}")
            cleaned, usage, thinking = draft_generator.generate_draft(ed, action_context)

            if cleaned:
                db.insert_draft(email_id, user_id, cleaned)
                if usage:
                    db.record_token_usage(user_id, "sonnet", "draft", usage)
                logger.info(f"  Draft inserted ({len(cleaned)} chars)")
            else:
                logger.error(f"  Draft generation FAILED for '{subject[:50]}'")

    logger.info("Backfill complete")


if __name__ == "__main__":
    db = SupabaseWorkerClient()
    backfill_drafts(db, email_ids=TARGET_EMAIL_IDS)
