"""Supabase I/O wrapper for the Railway pipeline worker.

Uses the service role key (full access, bypasses RLS).
All database operations for the worker go through this module.
"""

import logging
import os
from datetime import datetime

from supabase import create_client, Client

logger = logging.getLogger("worker")


class SupabaseWorkerClient:
    """Thin wrapper around supabase-py for worker-specific operations."""

    def __init__(self):
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.client: Client = create_client(url, key)

    # ------------------------------------------------------------------
    # User discovery
    # ------------------------------------------------------------------

    def get_users_with_unprocessed(self):
        """Get distinct user_ids that have unprocessed emails.

        Returns:
            list[str]: User IDs (UUIDs as strings).
        """
        result = (
            self.client.table("emails")
            .select("user_id")
            .eq("status", "unprocessed")
            .execute()
        )
        # Deduplicate
        seen = set()
        user_ids = []
        for row in result.data:
            uid = row["user_id"]
            if uid not in seen:
                seen.add(uid)
                user_ids.append(uid)
        return user_ids

    # ------------------------------------------------------------------
    # Atomic claim
    # ------------------------------------------------------------------

    def claim_unprocessed_emails(self, user_id, limit=10):
        """Atomically claim unprocessed emails for a user via RPC.

        Sets status='processing' and returns the claimed rows.
        Uses FOR UPDATE SKIP LOCKED to prevent duplicate processing.

        Args:
            user_id: UUID string.
            limit: Max emails to claim per batch.

        Returns:
            list[dict]: Claimed email rows.
        """
        result = self.client.rpc(
            "claim_unprocessed_emails",
            {"p_user_id": user_id, "p_limit": limit},
        ).execute()
        return result.data or []

    # ------------------------------------------------------------------
    # Profile / config
    # ------------------------------------------------------------------

    def fetch_user_config(self, user_id):
        """Fetch a user's profile configuration.

        Returns:
            dict: Profile row, or empty dict if not found.
        """
        result = (
            self.client.table("profiles")
            .select("*")
            .eq("id", user_id)
            .single()
            .execute()
        )
        return result.data or {}

    # ------------------------------------------------------------------
    # Email status
    # ------------------------------------------------------------------

    def update_email_status(self, email_id, status):
        """Update an email's processing status.

        Args:
            email_id: UUID string.
            status: One of 'unprocessed', 'processing', 'completed', 'error'.
        """
        self.client.table("emails").update(
            {"status": status}
        ).eq("id", email_id).execute()

    def bulk_update_email_status(self, email_ids, status):
        """Update status for multiple emails."""
        for eid in email_ids:
            self.update_email_status(eid, status)

    # ------------------------------------------------------------------
    # Classifications
    # ------------------------------------------------------------------

    def insert_classification(self, email_id, user_id, classification):
        """Insert a classification result for an email.

        Args:
            email_id: UUID string.
            user_id: UUID string.
            classification: dict with keys: needs_response, action, context,
                project, priority.
        """
        row = {
            "email_id": email_id,
            "user_id": user_id,
            "needs_response": classification.get("needs_response", False),
            "action": classification.get("action", ""),
            "context": classification.get("context", ""),
            "project": classification.get("project", ""),
            "priority": classification.get("priority", 0),
        }
        self.client.table("classifications").insert(row).execute()

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------

    def insert_draft(self, email_id, user_id, draft_body):
        """Insert or update a draft with status='pending'.

        Skips overwrite if the user has edited the draft via the dashboard
        (user_edited=true). The extension's Realtime subscription will pick
        up new/updated drafts and write them back to Outlook.

        Args:
            email_id: UUID string.
            user_id: UUID string.
            draft_body: The generated reply text.

        Returns:
            dict: The inserted/updated draft row, or empty dict if skipped.
        """
        # Check for existing draft
        existing_result = (
            self.client.table("drafts")
            .select("id, user_edited")
            .eq("email_id", email_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        existing = existing_result.data[0] if existing_result.data else None

        if existing and existing.get("user_edited"):
            logger.info(f"Skipping draft for email {email_id}: user has edited it")
            return {}

        if existing:
            # Update existing draft (worker re-run)
            result = (
                self.client.table("drafts")
                .update({"draft_body": draft_body, "status": "pending", "user_edited": False})
                .eq("id", existing["id"])
                .execute()
            )
        else:
            result = self.client.table("drafts").insert({
                "email_id": email_id,
                "user_id": user_id,
                "draft_body": draft_body,
                "status": "pending",
                "user_edited": False,
            }).execute()

        return result.data[0] if result.data else {}

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def fetch_conversation_context(self, user_id, conversation_id):
        """Fetch conversation messages for draft context.

        Returns:
            list[dict]: Message list from conversations.messages jsonb,
                or empty list if not found.
        """
        result = (
            self.client.table("conversations")
            .select("messages")
            .eq("user_id", user_id)
            .eq("conversation_id", conversation_id)
            .limit(1)
            .execute()
        )
        row = result.data[0] if result.data else None
        if row and row.get("messages"):
            return row["messages"]
        return []

    # ------------------------------------------------------------------
    # Pipeline runs
    # ------------------------------------------------------------------

    def create_pipeline_run(self, user_id, trigger_type="scheduled"):
        """Create a new pipeline_run log entry.

        Returns:
            str: The pipeline_run UUID.
        """
        row = {
            "user_id": user_id,
            "trigger_type": trigger_type,
            "status": "running",
            "started_at": datetime.utcnow().isoformat(),
        }
        result = self.client.table("pipeline_runs").insert(row).execute()
        return result.data[0]["id"] if result.data else None

    def update_pipeline_run(self, run_id, **kwargs):
        """Update a pipeline_run entry.

        Accepted kwargs: status, emails_scanned, emails_processed,
        drafts_generated, log_output, error_message, finished_at.
        """
        if not run_id:
            return
        update = {k: v for k, v in kwargs.items() if v is not None}
        if "finished_at" not in update and update.get("status") in ("completed", "failed"):
            update["finished_at"] = datetime.utcnow().isoformat()
        self.client.table("pipeline_runs").update(update).eq("id", run_id).execute()
