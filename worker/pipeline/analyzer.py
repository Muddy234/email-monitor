"""Claude email analysis — supports both Anthropic API and Claude CLI backends."""

import json
import logging
import re
import subprocess

from .prompts import DEFAULT_ANALYSIS_PROMPT

logger = logging.getLogger("email_monitor")

CLAUDE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "email_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email_index": {
                        "type": "integer",
                        "description": "1-based index matching the EMAIL N header"
                    },
                    "action": {
                        "type": "string",
                        "description": "Concise action description"
                    },
                    "date": {
                        "type": "string",
                        "description": "YYYY-MM-DD format"
                    },
                    "priority": {
                        "type": "string",
                        "description": "x if urgent, empty string otherwise"
                    },
                    "from_name": {
                        "type": "string",
                        "description": "Sender display name"
                    },
                    "project": {
                        "type": "string",
                        "description": "Canonical project name"
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject line"
                    },
                    "context": {
                        "type": "string",
                        "description": "Summary of the email"
                    },
                    "needs_response": {
                        "type": "boolean",
                        "description": "True if this email requires a reply from the user"
                    }
                },
                "required": ["email_index", "action", "date", "priority", "from_name", "project", "subject", "context", "needs_response"],
                "additionalProperties": False
            }
        }
    },
    "required": ["email_actions"],
    "additionalProperties": False
}


class ClaudeAnalyzer:
    """Sends batches of emails to Claude for structured analysis.

    Supports two backends controlled by config["claude_backend"]:
      - "api" (default): Anthropic Messages API via pipeline.api_client
      - "cli": Claude CLI subprocess via pipeline.cli (local fallback)
    """

    def __init__(self, config, system_prompt=None):
        self.backend = config.get("claude_backend", "api")
        self.model = config.get("claude_cli_model", "sonnet")
        self.timeout = config.get("claude_cli_timeout_seconds", 120)
        self.max_body_chars = config.get("max_body_chars", 5000)
        self.system_prompt = system_prompt or DEFAULT_ANALYSIS_PROMPT
        self.enable_signals = config.get("enable_response_signals", True)

        # CLI-specific settings (only used when backend == "cli")
        self.cli_path = config.get("claude_cli_path", "claude")
        self.max_budget = config.get("claude_cli_max_budget_usd", 0.50)

        # API-specific settings (only used when backend == "api")
        self.api_key = config.get("anthropic_api_key")  # None → env var

    def _build_prompt(self, email_batch):
        """Build the user prompt for a batch of emails."""
        parts = ["Analyze the following emails and return structured action items for each.\n"]

        for i, email_data in enumerate(email_batch, 1):
            subject = email_data.get("subject", "(no subject)")
            sender_name = email_data.get("sender_name", "Unknown")
            sender = email_data.get("sender", "")
            received = email_data.get("received_time")
            folder = email_data.get("folder", "Inbox")
            attachments = email_data.get("attachment_names", [])
            body = email_data.get("body", "") or ""

            date_str = received.strftime("%Y-%m-%d %H:%M") if received else "unknown"
            attach_str = ", ".join(attachments) if attachments else "None"

            if len(body) > self.max_body_chars:
                body = body[:self.max_body_chars] + "\n[... truncated]"

            parts.append(f"=== EMAIL {i} ===")
            thread_count = email_data.get("thread_count", 1)
            if thread_count > 1:
                conv_topic = email_data.get("conversation_topic", "")
                parts.append(f"Thread: {thread_count} messages in conversation \"{conv_topic}\"")
                parts.append(f"(This is the latest message. Earlier messages are quoted in the body.)")
            parts.append(f"Subject: {subject}")
            parts.append(f"From: {sender_name} <{sender}>")
            parts.append(f"Date: {date_str}")
            parts.append(f"Folder: {folder}")
            parts.append(f"Attachments: {attach_str}")

            # Inject response signals if available and enabled
            if self.enable_signals:
                signal_block = self._format_signal_block(email_data)
                if signal_block:
                    parts.append(signal_block)

            parts.append(f"\n{body}\n")

        parts.append("=== END ===")
        return "\n".join(parts)

    def _format_signal_block(self, email_data):
        """Build the RESPONSE SIGNALS text block for a single email.

        Renders all 6 signals with explicit null-state text when data is
        unavailable. Returns None if no signals are available.
        """
        signals = email_data.get("signals")
        if not signals:
            return None

        lines = ["--- RESPONSE SIGNALS ---"]

        # Signal 1: User Position
        pos = signals.get("user_position", "UNKNOWN")
        to_count = signals.get("to_count", 0)
        cc_count = signals.get("cc_count", 0)
        total = signals.get("total_recipients", 0)
        if pos == "UNKNOWN":
            lines.append("User Position: UNKNOWN (recipient data unavailable)")
        else:
            lines.append(f"User Position: {pos} (To: {to_count}, CC: {cc_count}, Total: {total})")

        # Signal 2: Name Mention
        mentioned = signals.get("user_mentioned_by_name", False)
        if mentioned:
            ctx = signals.get("name_mention_context", "")
            lines.append(f'Name Mentioned: YES — "{ctx}"')
        else:
            lines.append("Name Mentioned: NO")

        # Signal 3: Thread History
        thread_count = signals.get("thread_message_count")
        user_replies = signals.get("user_replies_in_thread", 0)
        user_active = signals.get("user_active_in_thread", False)
        if thread_count and thread_count > 1:
            active_str = "active" if user_active else "not yet replied"
            lines.append(f"Thread: {thread_count} messages, user replied {user_replies} times ({active_str})")
        else:
            lines.append("Thread: Single message (no thread data)")

        # Signal 4: Intent Classification
        intent = signals.get("intent_category", "unclassified")
        in_new = signals.get("intent_in_new_content", False)
        if intent != "unclassified":
            content_note = "in new content" if in_new else "in quoted content"
            lines.append(f"Intent: {intent} ({content_note})")
        else:
            lines.append("Intent: unclassified")

        # Signal 5: Conditional Response Rate
        rate = signals.get("sender_conditional_response_rate")
        sender_count = signals.get("sender_emails_last_30d")
        if rate is not None and sender_count:
            lines.append(f"Response Rate: {int(rate * 100)}% ({sender_count} emails/30d)")
        elif sender_count == 0:
            lines.append("Response Rate: Insufficient data (new or infrequent sender)")
        else:
            lines.append("Response Rate: Insufficient data (new or infrequent sender)")

        # Signal 6: Thread Velocity
        velocity = signals.get("thread_velocity")
        sub_count = signals.get("subsequent_replies_count", 0)
        sub_unique = signals.get("unique_subsequent_responders", 0)
        if velocity == "too_early":
            lines.append("Velocity: Too early to assess (email received <2 hours ago)")
        elif velocity == "none":
            lines.append("Velocity: None (no subsequent replies)")
        elif velocity in ("low", "medium", "high"):
            lines.append(f"Velocity: {velocity} ({sub_count} replies, {sub_unique} responders)")
        else:
            lines.append("Velocity: No thread data")

        # Importance (from Outlook)
        importance = email_data.get("importance", 1)
        importance_map = {0: "Low", 1: "Normal", 2: "High"}
        imp_label = importance_map.get(importance, "Normal")
        if imp_label != "Normal":
            lines.append(f"Importance Flag: {imp_label}")

        # Contact profiles (sender + recipients)
        sender_contact = email_data.get("sender_contact")
        if sender_contact and sender_contact.get("name"):
            parts = [sender_contact["name"]]
            if sender_contact.get("role"):
                parts.append(sender_contact["role"])
            if sender_contact.get("organization"):
                parts.append(sender_contact["organization"])
            ctype = sender_contact.get("contact_type", "unknown")
            parts.append(f"({ctype})")
            lines.append(f"Sender: {', '.join(parts)}")
        else:
            lines.append("Sender: Not in contact directory")

        recipient_contacts = email_data.get("recipient_contacts", {})
        if recipient_contacts:
            recip_parts = []
            for addr, profile in recipient_contacts.items():
                name = profile.get("name") or addr
                role = profile.get("role", "")
                org = profile.get("organization", "")
                ctype = profile.get("contact_type", "unknown")
                desc = name
                if role:
                    desc += f", {role}"
                if org:
                    desc += f", {org}"
                desc += f" ({ctype})"
                recip_parts.append(desc)
            lines.append(f"Other Recipients: {'; '.join(recip_parts)}")

        lines.append("--- END SIGNALS ---")
        return "\n".join(lines)

    def analyze_batch(self, email_batch):
        """Send a batch of emails to Claude, return list of action item dicts.

        Routes to the API or CLI backend based on self.backend.
        """
        if self.backend == "cli":
            return self._analyze_via_cli(email_batch)
        return self._analyze_via_api(email_batch)

    # ------------------------------------------------------------------
    # API backend
    # ------------------------------------------------------------------

    def _analyze_via_api(self, email_batch):
        """Analyze emails via the Anthropic Messages API."""
        from .api_client import call_claude, resolve_model

        prompt_text = self._build_prompt(email_batch)

        system = (
            self.system_prompt
            + "\n\nYou are a text analysis assistant. "
            "Simply analyze the emails provided and respond with valid JSON "
            "matching the schema. Output ONLY the JSON object, no other text.\n\n"
            + json.dumps(CLAUDE_JSON_SCHEMA, indent=2)
        )

        try:
            raw_output = call_claude(
                prompt=prompt_text,
                system_prompt=system,
                model=resolve_model(self.model),
                max_tokens=8192,
                timeout=self.timeout,
                api_key=self.api_key,
            )
        except Exception as e:
            logger.error(f"  Anthropic API call failed: {e}")
            return None

        if not raw_output:
            logger.warning("  Empty response from Anthropic API")
            return None

        actions = self._extract_json_from_text(raw_output)
        if actions is not None:
            return actions

        logger.error("  Could not extract JSON from Claude API response")
        logger.debug(f"  Response ({len(raw_output)} chars): {raw_output[:500]}")
        return None

    # ------------------------------------------------------------------
    # CLI backend (local fallback)
    # ------------------------------------------------------------------

    def _analyze_via_cli(self, email_batch):
        """Analyze emails via Claude CLI subprocess (original implementation)."""
        from .cli import run_claude_cli

        prompt_text = self._build_prompt(email_batch)
        user_prompt = self.system_prompt + "\n\n" + prompt_text

        append_prompt = (
            "You are a text analysis assistant. Do NOT use any tools. Do NOT read any files. "
            "Simply analyze the emails provided in the user message and respond with valid JSON "
            "matching the schema. Output ONLY the JSON object, no other text.\n\n"
            + json.dumps(CLAUDE_JSON_SCHEMA, indent=2)
        )

        cmd = [
            self.cli_path,
            "--print",
            "--model", self.model,
            "--max-budget-usd", str(self.max_budget),
            "--dangerously-skip-permissions",
            "--disallowedTools", "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,TodoWrite,NotebookEdit,Task,AskUserQuestion,Skill,EnterPlanMode,ExitPlanMode,KillShell,TaskOutput",
            "--append-system-prompt", append_prompt,
            "-p", "-",
        ]

        try:
            stdout, stderr, returncode = run_claude_cli(cmd, timeout=self.timeout, stdin_text=user_prompt)
        except subprocess.TimeoutExpired:
            logger.error(f"  Claude CLI timed out after {self.timeout}s")
            return None
        except FileNotFoundError:
            import os
            logger.error(f"  Claude CLI not found at: {self.cli_path}")
            logger.error(f"  File exists check: {os.path.isfile(self.cli_path)}")
            logger.error(f"  GIT_BASH_PATH: {os.environ.get('CLAUDE_CODE_GIT_BASH_PATH', 'NOT SET')}")
            logger.error(f"  Phase 3 (analysis) will be skipped.")
            return None
        except Exception as e:
            logger.error(f"  Claude CLI call failed: {e}")
            return None

        if returncode != 0:
            stderr_preview = stderr[:500]
            logger.warning(f"  Claude CLI returned code {returncode}")
            if stderr_preview:
                logger.warning(f"  stderr: {stderr_preview}")
            return None

        raw_output = stdout

        if not raw_output:
            logger.debug(f"  stdout is empty")
            logger.debug(f"  stderr: {stderr[:500]}")
            return None

        actions = self._extract_json_from_text(raw_output)
        if actions is not None:
            return actions

        logger.error(f"  Could not extract JSON from Claude response")
        logger.debug(f"  stdout ({len(raw_output)} chars): {raw_output[:500]}")
        return None

    # ------------------------------------------------------------------
    # JSON extraction (shared by both backends)
    # ------------------------------------------------------------------

    def _extract_json_from_text(self, text):
        """Extract email_actions from Claude's plain text response."""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed.get("email_actions", [])
        except json.JSONDecodeError:
            pass

        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                return parsed.get("email_actions", [])
            except json.JSONDecodeError:
                pass

        match = re.search(r'\{[^{}]*"email_actions".*\}', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed.get("email_actions", [])
            except json.JSONDecodeError:
                pass

        return None
