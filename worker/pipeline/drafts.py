"""Draft email reply generation via Anthropic API."""

import logging
import re

from .pre_process import isolate_new_content, truncate_smart

logger = logging.getLogger("worker")


class DraftGenerator:
    """Generates draft email replies via the Anthropic Messages API."""

    def __init__(self, config, system_prompt_template=None):
        self.model = config.get("draft_model", "opus")
        self.timeout = config.get("draft_cli_timeout_seconds", 90)
        self.user_name = config.get("draft_user_name", "")
        self.api_key = config.get("anthropic_api_key")  # None → env var

    def _build_draft_prompt(self, email_data, action_context, revision_notes=None):
        """Build the user prompt for draft generation.

        Structured as: PERSONALITY PROFILE → NEVER → EMAIL → THREAD SUMMARY
        → CONTACT SUMMARY → "Draft a reply."
        """
        sections = []

        # 1. Personality profile
        sections.append(self._build_personality_profile(action_context))

        # 2. NEVER guardrails
        sections.append(self._get_never_list())

        # 3. Email section
        sections.append(self._build_email_section(email_data, action_context))

        # 4. Thread summary
        thread_summary = action_context.get("thread_summary", "No prior thread history.")
        sections.append(f"THREAD SUMMARY:\n{thread_summary}")

        # 5. Contact summary
        sections.append(self._build_contact_summary(email_data))

        # 6. Instruction
        sections.append(
            "Draft a reply. If no response is needed, output only: "
            "NO_DRAFT_NEEDED: <reason>"
        )

        prompt = "\n\n".join(sections)

        if revision_notes:
            prompt += f"\n\n{revision_notes}"

        logger.debug(f"Draft prompt assembled: {len(prompt)} chars")

        return prompt

    @staticmethod
    def _build_personality_profile(action_context):
        """Return the Opus-aggregated personality blurb, or fall back to concat.

        Prefers the pre-aggregated `personality_blurb` (produced by Opus during
        onboarding). Falls back to concatenating style_guide + behavioral_profile
        + preference_profile when the blurb is missing.

        Returns "No personality profile available." if nothing is available.
        """
        personality_blurb = action_context.get("personality_blurb", "")
        if personality_blurb:
            return "PERSONALITY PROFILE:\n" + personality_blurb

        parts = []

        style_guide = action_context.get("style_guide", "")
        if style_guide:
            parts.append(style_guide)

        behavioral_profile = action_context.get("behavioral_profile", "")
        if behavioral_profile:
            parts.append(behavioral_profile)

        preference_profile = action_context.get("preference_profile", "")
        if preference_profile:
            # preference_profile is now plain text (string)
            if isinstance(preference_profile, str):
                parts.append(preference_profile)

        if not parts:
            return "PERSONALITY PROFILE:\nNo personality profile available."

        return "PERSONALITY PROFILE:\n" + "\n\n".join(parts)

    @staticmethod
    def _get_never_list():
        """Return static NEVER guardrails."""
        return (
            "NEVER:\n"
            "- Never fabricate information.\n"
            "- Never restate the sender's question back to them.\n"
            "- Never sign-off or act on behalf of anyone other than the USER.\n"
            "- Never answer on behalf of another person or an item that is "
            "outside of the USER authority; either produce no draft or "
            "acknowledge that the question is for the other person.\n"
            "- Never commit to a dollar amount, interest rate, loan term, or "
            "financial structure not in the email or thread.\n"
            "- Never make a legal commitment outside the confines of the email "
            "or thread.\n"
            "- Never fabricate a deadline, date, or timeline not in the email "
            "or thread.\n"
            "- Never address the recipient by the wrong name; use no greeting "
            "rather than guessing.\n"
            "- Never produce a draft longer than the situation requires."
        )

    @staticmethod
    def _build_email_section(email_data, action_context):
        """Build the EMAIL section with headers and isolated body."""
        subject = email_data.get("subject", "(no subject)")
        sender_name = email_data.get("sender_name", "Unknown")
        sender = email_data.get("sender", "")
        received_time = email_data.get("received_time", "")
        to_field = email_data.get("to", "")
        cc_field = email_data.get("cc", "")
        raw_body = email_data.get("body", "") or ""

        thread_emails = action_context.get("thread_emails", [])
        current_id = email_data.get("_db_id")
        prior_bodies = [
            te["body"] for te in thread_emails
            if te.get("body") and te.get("id") != current_id
        ]
        is_forward = subject.lower().startswith(("fw:", "fwd:"))
        body = isolate_new_content(raw_body, prior_bodies, subject=subject)
        body = truncate_smart(body, max_tokens=2500 if is_forward else 1000)

        lines = [
            "EMAIL:",
            f"From: {sender_name} <{sender}>",
            f"Sent: {received_time}",
        ]
        if to_field:
            lines.append(f"To: {to_field}")
        if cc_field:
            lines.append(f"Cc: {cc_field}")
        lines.append(f"Subject: {subject}")
        lines.append(f"\n{body}")

        return "\n".join(lines)

    @staticmethod
    def _build_contact_summary(email_data):
        """Build a CONTACT SUMMARY for all participants on the email.

        Formats each contact as a compact one-liner. Uses all_contacts dict
        (keyed by email address) populated by the pipeline.
        """
        all_contacts = email_data.get("all_contacts", {})

        if not all_contacts:
            # Fallback to sender_contact for backward compatibility
            sender_contact = email_data.get("sender_contact", {})
            if sender_contact:
                all_contacts = {email_data.get("sender", ""): sender_contact}

        if not all_contacts:
            return "CONTACT SUMMARY:\nUnknown sender — no contact record."

        lines = []
        for email_addr, contact in all_contacts.items():
            name = contact.get("name", "")
            ctype = contact.get("contact_type", "unknown")
            org = contact.get("organization", "")
            role = contact.get("role", "")
            significance = contact.get("relationship_significance", "")
            expertise = contact.get("expertise_areas") or []
            rel_summary = contact.get("relationship_summary") or ""

            parts = []
            if name:
                parts.append(f"{name} <{email_addr}>")
            else:
                parts.append(email_addr)

            desc = []
            if ctype and ctype != "unknown":
                desc.append(ctype.replace("_", " ").title())
            if org:
                desc.append(f"at {org}")
            if role:
                desc.append(f"| {role}")
            if significance and significance != "medium":
                desc.append(f"| Relationship: {significance}")

            if desc:
                parts.append(" — " + " ".join(desc))

            lines.append("".join(parts))

            if expertise:
                lines.append(f"  Expertise: {', '.join(expertise)}")
            if rel_summary:
                lines.append(f"  Relationship summary: {rel_summary}")

        return (
            "CONTACT SUMMARY (synthesized from prior interactions — "
            "may be outdated):\n" + "\n".join(lines)
        )

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
                "max_tokens": 2048,
                "temperature": 0.3,
                "messages": [{"role": "user", "content": prompt_text}],
            },
        }

    def generate_draft(self, email_data, action_context, revision_notes=None):
        """Generate a draft via the Anthropic Messages API.

        Args:
            email_data: Email data dict.
            action_context: Dict with classification and enrichment context.
            revision_notes: Optional QC feedback for retry attempts.

        Returns:
            tuple: (cleaned_draft, usage_dict, thinking_text)
                thinking_text is the raw chain-of-thought or None.
        """
        from .api_client import call_claude, resolve_model

        prompt_text = self._build_draft_prompt(
            email_data, action_context, revision_notes=revision_notes,
        )

        try:
            raw_output, usage = call_claude(
                prompt=prompt_text,
                model=resolve_model(self.model),
                max_tokens=2048,
                timeout=self.timeout,
                api_key=self.api_key,
                temperature=0.3,
            )
        except Exception as e:
            subject = email_data.get("subject", "unknown")
            logger.error(f"  Draft generation API call failed for '{subject}': {e}")
            return None, {}, None

        # Extract thinking before validation strips it
        thinking = self._extract_thinking(raw_output)
        if thinking:
            logger.debug(f"  Draft thinking ({len(thinking)} chars)")

        result = self._validate_output(raw_output, email_data)
        return result, usage, thinking

    @staticmethod
    def _extract_thinking(raw_output):
        """Extract the <thinking> block from raw model output.

        Returns the plain text inside the tags, or None if no thinking block.
        """
        if not raw_output:
            return None
        match = re.search(r"<thinking>(.*?)</thinking>", raw_output, flags=re.DOTALL)
        return match.group(1).strip() if match else None

    def _validate_output(self, raw_output, email_data):
        """Validate draft output. Returns cleaned text or None.

        Strips <thinking> tags from model output before validation.
        The thinking block is used for chain-of-thought reasoning during
        generation but must not appear in stored drafts or the extension UI.
        """
        if not raw_output:
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Empty output for '{subject}'")
            return None

        # Strip chain-of-thought <thinking> block (non-greedy to avoid
        # eating the draft body if the model emits malformed tags).
        cleaned = re.sub(r"<thinking>.*?</thinking>", "", raw_output, flags=re.DOTALL)

        # Fallback: if a lone <thinking> tag remains (unclosed), strip
        # everything from the start through the tag.
        if "<thinking>" in cleaned:
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Malformed <thinking> tag in draft for '{subject}', stripping prefix")
            cleaned = re.sub(r"^.*<thinking>", "", cleaned, flags=re.DOTALL)

        cleaned = cleaned.strip()

        # Check for Sonnet rejection marker
        if cleaned.startswith("NO_DRAFT_NEEDED:"):
            reason = cleaned[len("NO_DRAFT_NEEDED:"):].strip()
            subject = email_data.get("subject", "unknown")
            logger.info(f"  Draft REJECTED by model for '{subject}': {reason}")
            return None

        if len(cleaned) < 20:
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Draft too short for '{subject}' ({len(cleaned)} chars)")
            return None

        # Check for API error messages masquerading as drafts.
        # Require both an error-like prefix AND no greeting/sign-off
        # to avoid false positives on drafts like "Error-free closing..."
        error_prefixes = ["Error:", "Error -", "Usage:", "claude:", "CRITICAL:", "WARNING:"]
        if any(cleaned.startswith(prefix) for prefix in error_prefixes):
            has_sign_off = bool(re.search(
                r'(?:regards|thanks|best|sincerely|cheers)\s*[,.]?\s*\n',
                cleaned, re.IGNORECASE,
            ))
            if not has_sign_off:
                subject = email_data.get("subject", "unknown")
                logger.warning(f"  Draft appears to be an error message for '{subject}'")
                return None

        return cleaned
