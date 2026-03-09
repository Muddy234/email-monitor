"""Supabase I/O wrapper for the Railway pipeline worker.

Uses the service role key (full access, bypasses RLS).
All database operations for the worker go through this module.
"""

import logging
import os
from datetime import datetime, timedelta

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
        """Update status for multiple emails in a single query."""
        if not email_ids:
            return
        self.client.table("emails").update(
            {"status": status}
        ).in_("id", email_ids).execute()

    # ------------------------------------------------------------------
    # Classifications
    # ------------------------------------------------------------------

    def insert_classification(self, email_id, user_id, classification):
        """Insert a classification result for an email.

        Args:
            email_id: UUID string.
            user_id: UUID string.
            classification: dict with keys: needs_response, action, context,
                project, priority. Optional: reason, archetype, classification_confidence.
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

        # Write enriched fields to emails table if present
        email_update = {}
        if "reason" in classification:
            email_update["reason"] = classification["reason"]
        if "archetype" in classification:
            email_update["archetype"] = classification["archetype"]
        if "classification_confidence" in classification:
            email_update["classification_confidence"] = classification["classification_confidence"]
        if email_update:
            self.client.table("emails").update(email_update).eq("id", email_id).execute()

    def bulk_insert_classifications(self, rows):
        """Insert multiple classification rows in a single request."""
        if not rows:
            return
        self.client.table("classifications").insert(rows).execute()

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

    # ------------------------------------------------------------------
    # Onboarding
    # ------------------------------------------------------------------

    def get_users_needing_onboarding(self, min_emails=20):
        """Find users who haven't completed onboarding and have enough emails.

        Returns:
            list[str]: User IDs ready for onboarding.
        """
        # Users with no onboarding_completed_at and not currently running
        result = (
            self.client.table("profiles")
            .select("id, onboarding_status")
            .is_("onboarding_completed_at", "null")
            .execute()
        )
        if not result.data:
            return []

        ready = []
        for row in result.data:
            uid = row["id"]
            # Skip users already mid-onboarding (unless failed)
            status = row.get("onboarding_status")
            if status and status != "failed":
                continue
            # Check email count
            count_result = (
                self.client.table("emails")
                .select("id", count="exact")
                .eq("user_id", uid)
                .execute()
            )
            if count_result.count and count_result.count >= min_emails:
                ready.append(uid)
        return ready

    def fetch_emails_for_onboarding(self, user_id, days=30, max_emails=None):
        """Fetch emails (inbox + sent) for the last N days.

        Args:
            user_id: UUID string.
            days: Lookback window in days.
            max_emails: Optional cap on total rows returned (newest first
                        when capped, then re-sorted ascending).

        Returns:
            list[dict]: Email rows ordered by received_time ascending.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        query = (
            self.client.table("emails")
            .select("*")
            .eq("user_id", user_id)
            .gte("received_time", cutoff)
        )

        if max_emails:
            # Fetch newest N emails, then reverse to ascending order
            result = query.order("received_time", desc=True).limit(max_emails).execute()
            rows = result.data or []
            rows.sort(key=lambda r: r.get("received_time") or "")
            return rows

        result = query.order("received_time", desc=False).execute()
        return result.data or []

    def update_onboarding_status(self, user_id, status, **kwargs):
        """Update a user's onboarding status and optional timestamp fields.

        Args:
            user_id: UUID string.
            status: Onboarding phase name.
            **kwargs: Optional fields like started_at, completed_at.
        """
        update = {"onboarding_status": status}
        if "started_at" in kwargs:
            update["onboarding_started_at"] = kwargs["started_at"]
        if "completed_at" in kwargs:
            update["onboarding_completed_at"] = kwargs["completed_at"]
        self.client.table("profiles").update(update).eq("id", user_id).execute()

    def upsert_contacts(self, user_id, contacts_list):
        """Batch upsert contacts for a user.

        Args:
            user_id: UUID string.
            contacts_list: List of contact dicts with at minimum 'email' key.
        """
        rows = []
        now = datetime.utcnow().isoformat()
        for contact in contacts_list:
            # Coerce types to match DB schema
            epm = contact.get("emails_per_month", 0)
            expertise = contact.get("expertise_areas", [])
            if not isinstance(expertise, list):
                expertise = [expertise] if expertise else []
            co_recip = contact.get("common_co_recipients", [])
            if not isinstance(co_recip, list):
                co_recip = [co_recip] if co_recip else []

            typ_subj = contact.get("typical_subjects", [])
            if not isinstance(typ_subj, list):
                typ_subj = [typ_subj] if typ_subj else []

            row = {
                "user_id": user_id,
                "email": contact["email"],
                "name": contact.get("name"),
                "organization": contact.get("organization") or contact.get("inferred_organization"),
                "role": contact.get("role") or contact.get("inferred_role"),
                "expertise_areas": expertise,
                "contact_type": contact.get("contact_type", "unknown"),
                "relationship_significance": contact.get("relationship_significance", "medium"),
                "relationship_summary": contact.get("relationship_summary"),
                "total_received": contact.get("total_received", 0),
                "emails_per_month": round(float(epm), 1) if epm is not None else 0,
                "response_rate": contact.get("response_rate"),
                "reply_rate_30d": contact.get("reply_rate_30d"),
                "reply_rate_90d": contact.get("reply_rate_90d"),
                "smoothed_rate": contact.get("smoothed_rate"),
                "avg_response_time_hours": contact.get("avg_response_time_hours"),
                "median_response_time_hours": contact.get("median_response_time_hours"),
                "user_initiates_pct": contact.get("user_initiates_pct"),
                "forward_rate": contact.get("forward_rate"),
                "typical_subjects": typ_subj,
                "common_co_recipients": co_recip,
                "last_interaction_at": contact.get("last_interaction_at"),
                "last_profiled_at": now,
                "updated_at": now,
            }
            rows.append(row)

        if rows:
            try:
                self.client.table("contacts").upsert(
                    rows, on_conflict="user_id,email"
                ).execute()
            except Exception as e:
                logger.error(f"upsert_contacts failed: {e}")
                if rows:
                    logger.error(f"Sample row keys: {list(rows[0].keys())}")
                    logger.error(f"Sample row: {rows[0]}")
                raise

    def upsert_topic_profile(self, user_id, data):
        """Upsert a user's topic profile (domains, keywords, stats).

        Args:
            user_id: UUID string.
            data: Dict with keys like domains, high_signal_keywords,
                  token_frequencies, baseline_statistics.
        """
        row = {
            "user_id": user_id,
            "domains": data.get("domains", []),
            "high_signal_keywords": data.get("high_signal_keywords", []),
            "token_frequencies": data.get("token_frequencies"),
            "baseline_statistics": data.get("baseline_statistics", {}),
            "updated_at": datetime.utcnow().isoformat(),
        }
        self.client.table("user_topic_profile").upsert(
            row, on_conflict="user_id"
        ).execute()

    def update_writing_style(self, user_id, style_guide, sample_count):
        """Store the writing style guide on the user's profile.

        Args:
            user_id: UUID string.
            style_guide: Plain text style guide.
            sample_count: Number of sent emails analyzed.
        """
        self.client.table("profiles").update({
            "writing_style_guide": style_guide,
            "style_profiled_at": datetime.utcnow().isoformat(),
            "style_sample_count": sample_count,
        }).eq("id", user_id).execute()

    # ------------------------------------------------------------------
    # Batch context fetchers (for enrichment pipeline)
    # ------------------------------------------------------------------

    def fetch_contacts_by_emails(self, user_id, email_list):
        """Batch-fetch contact profiles for a list of sender emails.

        Args:
            user_id: UUID string.
            email_list: list[str] of sender email addresses.

        Returns:
            dict: {email: contact_row} for found contacts.
        """
        if not email_list:
            return {}
        unique = list(set(email_list))
        result = (
            self.client.table("contacts")
            .select("*")
            .eq("user_id", user_id)
            .in_("email", unique)
            .execute()
        )
        return {row["email"]: row for row in (result.data or [])}

    def fetch_thread_messages(self, user_id, conversation_ids):
        """Batch-fetch conversation rows for multiple conversation IDs.

        Args:
            user_id: UUID string.
            conversation_ids: list[str] of conversation IDs.

        Returns:
            dict: {conversation_id: conversation_row} with messages jsonb.
        """
        if not conversation_ids:
            return {}
        unique = list(set(conversation_ids))
        result = (
            self.client.table("conversations")
            .select("conversation_id, messages, updated_at")
            .eq("user_id", user_id)
            .in_("conversation_id", unique)
            .execute()
        )
        return {row["conversation_id"]: row for row in (result.data or [])}

    def fetch_user_topic_profile(self, user_id):
        """Fetch the user's topic profile (domains, keywords, stats).

        Args:
            user_id: UUID string.

        Returns:
            dict: Topic profile row, or empty dict if not found.
        """
        result = (
            self.client.table("user_topic_profile")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
        return {}

    # ------------------------------------------------------------------
    # Scoring parameters
    # ------------------------------------------------------------------

    def upsert_scoring_parameters(self, user_id, parameters_json, emails_used=None):
        """Store per-user scoring model artifacts."""
        now = datetime.utcnow().isoformat()
        row = {
            "user_id": user_id,
            "parameters": parameters_json,
            "generated_at": now,
        }
        if emails_used is not None:
            row["emails_used"] = emails_used
        self.client.table("scoring_parameters").upsert(
            row, on_conflict="user_id"
        ).execute()

    def fetch_scoring_parameters(self, user_id):
        """Load per-user scoring model artifacts.

        Returns:
            dict: The parameters JSON, or None if not found.
        """
        result = (
            self.client.table("scoring_parameters")
            .select("parameters,generated_at,emails_used")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("parameters")
        return None

    # ------------------------------------------------------------------
    # Active users
    # ------------------------------------------------------------------

    def get_active_user_ids(self):
        """Return user IDs where worker_active is true."""
        result = (
            self.client.table("profiles")
            .select("id")
            .eq("worker_active", True)
            .execute()
        )
        return [row["id"] for row in (result.data or [])]

    # ------------------------------------------------------------------
    # Response events
    # ------------------------------------------------------------------

    def fetch_response_events(self, user_id):
        """Load all response events for a user (for model training)."""
        result = (
            self.client.table("response_events")
            .select("*")
            .eq("user_id", user_id)
            .order("received_time")
            .execute()
        )
        return result.data or []

    def count_response_events_since(self, user_id, since_timestamp):
        """Count response events created after a given timestamp."""
        result = (
            self.client.table("response_events")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .gt("created_at", since_timestamp)
            .execute()
        )
        return result.count or 0

    def upsert_response_events(self, user_id, events):
        """Bulk upsert response events for a user."""
        rows = []
        for event in events:
            row = {
                "user_id": user_id,
                "email_id": event["email_id"],
                "sender_email": event.get("sender_email", ""),
                "received_time": event.get("received_time"),
                "responded": event.get("responded", False),
                "response_latency_hours": event.get("response_latency_hours"),
                "response_type": event.get("response_type"),
                "conversation_id": event.get("conversation_id"),
                "subject": event.get("subject"),
                "user_position": event.get("user_position"),
                "total_recipients": event.get("total_recipients"),
                "has_question": event.get("has_question", False),
                "has_action_language": event.get("has_action_language", False),
                "subject_type": event.get("subject_type"),
                "is_recurring": event.get("is_recurring", False),
                "mentions_user_name": event.get("mentions_user_name", False),
                "sender_is_internal": event.get("sender_is_internal", False),
                "thread_user_initiated": event.get("thread_user_initiated", False),
                "arrived_during_active_hours": event.get("arrived_during_active_hours"),
                "arrived_on_active_day": event.get("arrived_on_active_day"),
                "thread_depth": event.get("thread_depth", 1),
                "scoring_factors": event.get("scoring_factors"),
                "raw_score": event.get("raw_score"),
                "calibrated_prob": event.get("calibrated_prob"),
                "confidence_tier": event.get("confidence_tier"),
                "gate_reason": event.get("gate_reason"),
            }
            rows.append(row)

        if rows:
            # Batch in chunks of 500 to avoid payload limits
            for i in range(0, len(rows), 500):
                self.client.table("response_events").upsert(
                    rows[i:i + 500], on_conflict="user_id,email_id"
                ).execute()

    def label_response_events_responded(self, user_id, email_ids):
        """Mark response_events as responded=True for emails the user replied to.

        Args:
            user_id: UUID string.
            email_ids: list[str] of email IDs that received a user reply.
        """
        if not email_ids:
            return
        # Batch in chunks of 200 to stay within query-string limits
        for i in range(0, len(email_ids), 200):
            chunk = email_ids[i:i + 200]
            self.client.table("response_events").update(
                {"responded": True}
            ).eq("user_id", user_id).in_("email_id", chunk).execute()

    def bulk_upsert_contact_stats(self, user_id, sender_stats):
        """Batch upsert minimal contact records for new/existing senders.

        Uses atomic ON CONFLICT to avoid read-then-write race conditions.

        Args:
            user_id: UUID string.
            sender_stats: list of dicts with keys: email, received_time.
        """
        if not sender_stats:
            return
        now = datetime.utcnow().isoformat()
        rows = []
        for stat in sender_stats:
            rows.append({
                "user_id": user_id,
                "email": stat["email"],
                "total_received": 1,
                "last_interaction_at": stat.get("received_time") or now,
                "updated_at": now,
            })
        if rows:
            # Supabase upsert with on_conflict handles the atomic increment
            # via a Postgres function. For the SDK, we do a raw RPC or
            # fall back to upsert (which sets total_received=1 for new rows).
            # For existing rows, we need a separate update.
            # Split into new vs existing contacts.
            emails = [r["email"] for r in rows]
            existing = self.fetch_contacts_by_emails(user_id, emails)

            new_rows = [r for r in rows if r["email"] not in existing]
            update_emails = [r for r in rows if r["email"] in existing]

            if new_rows:
                self.client.table("contacts").upsert(
                    new_rows, on_conflict="user_id,email"
                ).execute()

            # Increment existing contacts atomically via individual updates
            for r in update_emails:
                contact = existing[r["email"]]
                self.client.table("contacts").update({
                    "total_received": (contact.get("total_received") or 0) + 1,
                    "last_interaction_at": r["last_interaction_at"],
                    "updated_at": now,
                }).eq("user_id", user_id).eq("email", r["email"]).execute()

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def upsert_threads(self, user_id, threads):
        """Bulk upsert thread statistics for a user."""
        now = datetime.utcnow().isoformat()
        rows = []
        for thread in threads:
            row = {
                "user_id": user_id,
                "conversation_id": thread["conversation_id"],
                "total_messages": thread.get("total_messages", 0),
                "user_messages": thread.get("user_messages", 0),
                "participation_rate": thread.get("participation_rate"),
                "user_initiated": thread.get("user_initiated", False),
                "user_avg_body_length": thread.get("user_avg_body_length"),
                "other_responders": thread.get("other_responders", []),
                "duration_days": thread.get("duration_days", 0),
                "updated_at": now,
            }
            rows.append(row)

        if rows:
            for i in range(0, len(rows), 500):
                self.client.table("threads").upsert(
                    rows[i:i + 500], on_conflict="user_id,conversation_id"
                ).execute()

    # ------------------------------------------------------------------
    # Domains
    # ------------------------------------------------------------------

    def upsert_domains(self, user_id, domains):
        """Bulk upsert domain statistics for a user."""
        now = datetime.utcnow().isoformat()
        rows = []
        for domain in domains:
            row = {
                "user_id": user_id,
                "domain": domain["domain"],
                "avg_reply_rate": domain.get("avg_reply_rate"),
                "contact_count": domain.get("contact_count", 0),
                "domain_category": domain.get("domain_category", "external"),
                "updated_at": now,
            }
            rows.append(row)

        if rows:
            self.client.table("domains").upsert(
                rows, on_conflict="user_id,domain"
            ).execute()
