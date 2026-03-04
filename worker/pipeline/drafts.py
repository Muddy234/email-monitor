"""Draft email reply generation — supports both Anthropic API and Claude CLI backends."""

import logging
import subprocess

from .prompts import DEFAULT_DRAFT_PROMPT_TEMPLATE

logger = logging.getLogger("email_monitor")


class DraftGenerator:
    """Generates draft email replies via Claude.

    Supports two backends controlled by config["claude_backend"]:
      - "api" (default): Anthropic Messages API via pipeline.api_client
      - "cli": Claude CLI subprocess via pipeline.cli (local fallback)
    """

    def __init__(self, config, system_prompt_template=None):
        self.backend = config.get("claude_backend", "api")
        self.model = config.get("draft_cli_model", "sonnet")
        self.timeout = config.get("draft_cli_timeout_seconds", 90)
        self.user_name = config.get("draft_user_name", "")
        self.user_title = config.get("draft_user_title", "")

        template = system_prompt_template or DEFAULT_DRAFT_PROMPT_TEMPLATE
        self.system_prompt = template.format(
            user_name=self.user_name,
            user_title=self.user_title
        )

        # CLI-specific settings
        self.cli_path = config.get("claude_cli_path", "claude")

        # API-specific settings
        self.api_key = config.get("anthropic_api_key")  # None → env var

    def _build_draft_prompt(self, email_data, action_context):
        """Build the user prompt for draft generation."""
        subject = email_data.get("subject", "(no subject)")
        sender_name = email_data.get("sender_name", "Unknown")
        sender = email_data.get("sender", "")
        body = email_data.get("body", "") or ""
        action = action_context.get("action", "")
        context = action_context.get("context", "")

        # Build signal context for tone/style guidance
        signal_lines = []
        signals = email_data.get("signals", {})
        if signals:
            pos = signals.get("user_position")
            if pos:
                signal_lines.append(f"User Position: {pos}")
            intent = signals.get("intent_category")
            if intent and intent != "unclassified":
                signal_lines.append(f"Intent: {intent}")

        sender_contact = email_data.get("sender_contact", {})
        if sender_contact:
            ctype = sender_contact.get("contact_type", "")
            org = sender_contact.get("organization", "")
            if ctype:
                signal_lines.append(f"Sender Type: {ctype}")
            if org:
                signal_lines.append(f"Sender Org: {org}")

        signal_block = ""
        if signal_lines:
            signal_block = "\n\nSIGNAL CONTEXT (use for tone):\n" + "\n".join(signal_lines)
            signal_block += "\n\nNote: Use a more formal tone for external_legal and external_lender contacts. Use a conversational but professional tone for internal colleagues."

        prompt = f"""Draft a reply to the following email:

FROM: {sender_name} <{sender}>
SUBJECT: {subject}

EMAIL BODY:
{body}

ACTION NEEDED: {action}
CONTEXT: {context}{signal_block}

Generate the reply body text only (no subject, no headers)."""

        return prompt

    def generate_draft(self, email_data, action_context):
        """Generate a draft reply for an email. Returns draft_body string or None.

        Routes to the API or CLI backend based on self.backend.
        """
        if self.backend == "cli":
            return self._generate_via_cli(email_data, action_context)
        return self._generate_via_api(email_data, action_context)

    # ------------------------------------------------------------------
    # API backend
    # ------------------------------------------------------------------

    def _generate_via_api(self, email_data, action_context):
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
            )
        except Exception as e:
            subject = email_data.get("subject", "unknown")
            logger.error(f"  Draft generation API call failed for '{subject}': {e}")
            return None

        return self._validate_output(raw_output, email_data)

    # ------------------------------------------------------------------
    # CLI backend (local fallback)
    # ------------------------------------------------------------------

    def _generate_via_cli(self, email_data, action_context):
        """Generate a draft via Claude CLI subprocess (original implementation)."""
        from .cli import run_claude_cli

        prompt_text = self._build_draft_prompt(email_data, action_context)

        cmd = [
            self.cli_path,
            "--print",
            "--model", self.model,
            "--dangerously-skip-permissions",
            "--disallowedTools", "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,TodoWrite,NotebookEdit,Task,AskUserQuestion,Skill,EnterPlanMode,ExitPlanMode,KillShell,TaskOutput",
            "--append-system-prompt", self.system_prompt,
            "-p", "-",
        ]

        try:
            stdout, stderr, returncode = run_claude_cli(cmd, timeout=self.timeout, stdin_text=prompt_text)
        except subprocess.TimeoutExpired:
            subject = email_data.get("subject", "unknown")
            logger.error(f"  Draft generation timed out after {self.timeout}s for '{subject}'")
            return None
        except FileNotFoundError:
            logger.error(f"  Claude CLI not found at: {self.cli_path}")
            return None
        except Exception as e:
            subject = email_data.get("subject", "unknown")
            logger.error(f"  Draft generation failed for '{subject}': {e}")
            return None

        if returncode != 0:
            stderr_preview = stderr[:500]
            subject = email_data.get("subject", "unknown")
            logger.warning(f"  Draft generation failed for '{subject}': CLI returned code {returncode}")
            if stderr_preview:
                logger.warning(f"  stderr: {stderr_preview}")
            return None

        return self._validate_output(stdout, email_data)

    # ------------------------------------------------------------------
    # Output validation (shared by both backends)
    # ------------------------------------------------------------------

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
