"""Draft email reply generation via Anthropic API."""

import logging

from .analyzer import _strip_quoted_content
from .prompts import DEFAULT_DRAFT_PROMPT_TEMPLATE

logger = logging.getLogger("clarion")


class DraftGenerator:
    """Generates draft email replies via the Anthropic Messages API."""

    def __init__(self, config, system_prompt_template=None):
        self.model = config.get("draft_model", "sonnet")
        self.timeout = config.get("draft_cli_timeout_seconds", 90)
        self.user_name = config.get("draft_user_name", "")
        self.user_title = config.get("draft_user_title", "")

        template = system_prompt_template or DEFAULT_DRAFT_PROMPT_TEMPLATE
        self.system_prompt = template.format(
            user_name=self.user_name,
            user_title=self.user_title
        )

        self.api_key = config.get("anthropic_api_key")  # None → env var

    def _build_draft_prompt(self, email_data, action_context):
        """Build the user prompt for draft generation.

        Uses enrichment data (reason, archetype, sender/thread briefings)
        when available, falling back to basic action/context.
        """
        subject = email_data.get("subject", "(no subject)")
        sender_name = email_data.get("sender_name", "Unknown")
        sender = email_data.get("sender", "")
        body = _strip_quoted_content(email_data.get("body", "") or "")
        if len(body) > 4000:
            body = body[:4000] + "\n[... truncated]"

        # Check for enrichment data
        enrichment = action_context.get("enrichment")
        reason = action_context.get("reason", "")
        archetype = action_context.get("archetype", "")

        # Build context block — prefer enriched data when available
        context_lines = []

        if reason:
            context_lines.append(f"Why a response is needed: {reason}")
        else:
            action = action_context.get("action", "")
            context_text = action_context.get("context", "")
            if action:
                context_lines.append(f"ACTION NEEDED: {action}")
            if context_text:
                context_lines.append(f"CONTEXT: {context_text}")

        if archetype and archetype != "none":
            context_lines.append(f"Expected response type: {archetype}")

        if enrichment:
            sb = enrichment.get("sender_briefing", {})
            tb = enrichment.get("thread_briefing", {})
            if sb.get("summary"):
                context_lines.append(f"Sender context: {sb['summary']}")
            if tb.get("summary"):
                context_lines.append(f"Thread context: {tb['summary']}")

        # Tone guidance from contact type
        tone_lines = []
        sender_contact = email_data.get("sender_contact", {})
        if sender_contact:
            ctype = sender_contact.get("contact_type", "")
            org = sender_contact.get("organization", "")
            if ctype:
                tone_lines.append(f"Sender Type: {ctype}")
            if org:
                tone_lines.append(f"Sender Org: {org}")

        tone_block = ""
        if tone_lines:
            tone_block = "\n\nTONE GUIDANCE:\n" + "\n".join(tone_lines)
            tone_block += "\nNote: Use a more formal tone for external_legal and external_lender contacts. Use a conversational but professional tone for internal colleagues."

        context_block = "\n".join(context_lines)

        prompt = f"""Draft a reply to the following email:

FROM: {sender_name} <{sender}>
SUBJECT: {subject}

EMAIL BODY:
{body}

{context_block}{tone_block}

Generate the reply body text only (no subject, no headers)."""

        return prompt

    def build_batch_params(self, email_data, action_context, custom_id):
        """Build a Batches API request dict for a single draft.

        Args:
            email_data: Email data dict.
            action_context: Dict with 'action' and 'context' keys.
            custom_id: Unique ID for this request in the batch.

        Returns:
            dict with 'custom_id' and 'params' keys.
        """
        from .api_client import resolve_model

        prompt_text = self._build_draft_prompt(email_data, action_context)
        return {
            "custom_id": custom_id,
            "params": {
                "model": resolve_model(self.model),
                "max_tokens": 4096,
                "temperature": 0.3,
                "system": [
                    {"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}
                ],
                "messages": [{"role": "user", "content": prompt_text}],
            },
        }

    def generate_draft(self, email_data, action_context):
        """Generate a draft via the Anthropic Messages API."""
        from .api_client import call_claude, resolve_model

        prompt_text = self._build_draft_prompt(email_data, action_context)

        try:
            raw_output = call_claude(
                prompt=prompt_text,
                system_prompt=self.system_prompt,
                model=resolve_model(self.model),
                max_tokens=4096,
                timeout=self.timeout,
                api_key=self.api_key,
                temperature=0.3,
                cache_system_prompt=True,
            )
        except Exception as e:
            subject = email_data.get("subject", "unknown")
            logger.error(f"  Draft generation API call failed for '{subject}': {e}")
            return None

        return self._validate_output(raw_output, email_data)

    def _validate_output(self, raw_output, email_data):
        """Validate draft output. Returns cleaned text or None."""
        if not raw_output:
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Empty output for '{subject}'")
            return None

        if len(raw_output) < 20:
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Draft too short for '{subject}' ({len(raw_output)} chars)")
            return None

        error_prefixes = ["Error", "Usage:", "claude:", "CRITICAL:", "WARNING:"]
        if any(raw_output.startswith(prefix) for prefix in error_prefixes):
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Draft appears to be an error message for '{subject}'")
            return None

        return raw_output
