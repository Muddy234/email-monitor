"""Claude email analysis — supports both Anthropic API and Claude CLI backends."""

import json
import logging
import re
import subprocess

from .prompts import DEFAULT_ANALYSIS_PROMPT, ENRICHED_ANALYSIS_PROMPT

logger = logging.getLogger("clarion")

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
                    "needs_response": {
                        "type": "boolean",
                        "description": "Whether the user needs to reply to this email"
                    },
                    "action": {
                        "type": "string",
                        "description": "Concise action the user should take (1-2 sentences)"
                    },
                    "context": {
                        "type": "string",
                        "description": "Brief summary of the email content"
                    },
                    "project": {
                        "type": "string",
                        "description": "Canonical project name from the project list, or 'General'"
                    },
                    "priority": {
                        "type": "string",
                        "description": "'x' if urgent (deadline <1 week, URGENT keyword, signature/payment request), else empty string"
                    }
                },
                "required": ["email_index", "needs_response", "action", "context", "project", "priority"],
                "additionalProperties": False
            }
        }
    },
    "required": ["email_actions"],
    "additionalProperties": False
}

# Schema for enriched classification pipeline (reason + archetype + confidence)
ENRICHED_JSON_SCHEMA = {
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
                    "needs_response": {
                        "type": "boolean",
                        "description": "Whether the user needs to reply to this email"
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence in the needs_response decision (0.0-1.0)"
                    },
                    "reason": {
                        "type": "string",
                        "description": "1-2 sentence explanation of why the user does or doesn't need to respond"
                    },
                    "archetype": {
                        "type": "string",
                        "enum": ["acknowledgment", "substantive", "routing", "scheduling", "approval", "none"],
                        "description": "Type of response expected"
                    },
                    "priority": {
                        "type": "string",
                        "description": "'x' if urgent, else empty string"
                    },
                    "project": {
                        "type": "string",
                        "description": "Canonical project name from the project list, or 'General'"
                    }
                },
                "required": ["email_index", "needs_response", "confidence", "reason", "archetype", "priority", "project"],
                "additionalProperties": False
            }
        }
    },
    "required": ["email_actions"],
    "additionalProperties": False
}


def _strip_quoted_content(body):
    """Strip quoted reply chains, keeping only the newest message."""
    for marker in ["From:", "-----Original Message", "________________________________"]:
        idx = body.find(marker)
        if idx > 0:
            return body[:idx].rstrip()
    return body


def _validate_results(claude_results):
    """Validate each classification individually.

    Returns (valid_list, invalid_indices) so a single bad entry
    doesn't sink the whole batch.
    """
    valid = []
    invalid_indices = []

    for result in claude_results:
        if (
            isinstance(result.get("email_index"), int)
            and isinstance(result.get("needs_response"), bool)
            and isinstance(result.get("action"), str)
            and result.get("action", "").strip()
        ):
            valid.append(result)
        else:
            invalid_indices.append(result.get("email_index"))
            logger.warning(f"  Invalid classification for email {result.get('email_index')}: {result}")

    return valid, invalid_indices


def _validate_enriched_results(claude_results):
    """Validate enriched classification results (reason/archetype/confidence schema).

    Returns (valid_list, invalid_indices).
    """
    valid_archetypes = {"acknowledgment", "substantive", "routing", "scheduling", "approval", "none"}
    valid = []
    invalid_indices = []

    for result in claude_results:
        if (
            isinstance(result.get("email_index"), int)
            and isinstance(result.get("needs_response"), bool)
            and isinstance(result.get("reason"), str)
            and result.get("reason", "").strip()
            and result.get("archetype", "") in valid_archetypes
        ):
            # Clamp confidence to [0, 1]
            conf = result.get("confidence")
            if isinstance(conf, (int, float)):
                result["confidence"] = max(0.0, min(1.0, float(conf)))
            else:
                result["confidence"] = 0.5
            valid.append(result)
        else:
            invalid_indices.append(result.get("email_index"))
            logger.warning(f"  Invalid enriched classification for email {result.get('email_index')}: {result}")

    return valid, invalid_indices


def _merge_known_fields(claude_results, email_batch):
    """Merge pre-known email metadata back into Claude's classification results.

    Claude no longer returns from_name, subject, or date — we stitch them
    back in from the original email data so downstream consumers stay unchanged.
    """
    email_lookup = {i + 1: email for i, email in enumerate(email_batch)}

    for result in claude_results:
        email = email_lookup.get(result.get("email_index"))
        if email:
            result.setdefault("from_name", email.get("sender_name", email.get("sender", "")))
            result.setdefault("subject", email.get("subject", ""))
            result.setdefault("date", str(email.get("received_time", ""))[:10])

    return claude_results


class ClaudeAnalyzer:
    """Sends batches of emails to Claude for structured analysis.

    Supports two backends controlled by config["claude_backend"]:
      - "api" (default): Anthropic Messages API via pipeline.api_client
      - "cli": Claude CLI subprocess via pipeline.cli (local fallback)
    """

    def __init__(self, config, system_prompt=None):
        self.backend = config.get("claude_backend", "api")
        self.model = config.get("classification_model", "haiku")
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
            raw_body = email_data.get("body", "") or ""
            body = _strip_quoted_content(raw_body)

            date_str = str(received)[:16] if received else "unknown"
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

    def _build_system_prompt(self):
        """Build the full system prompt (schema + analysis instructions)."""
        return (
            "You must return ONLY a valid JSON object matching this exact schema:\n\n"
            + json.dumps(CLAUDE_JSON_SCHEMA, indent=2)
            + "\n\nOutput ONLY the raw JSON object. "
            "No markdown fences. No preamble. No explanation. "
            "Start your response with { and end with }.\n\n"
            + self.system_prompt
        )

    def build_batch_params(self, email_batch):
        """Build a Batches API request dict for a batch of emails.

        Returns a dict with 'custom_id' and 'params' keys, suitable for
        passing to api_client.create_message_batch().
        """
        from .api_client import resolve_model

        prompt_text = self._build_prompt(email_batch)
        system_text = self._build_system_prompt()
        scaled_max_tokens = min(2048, 256 + len(email_batch) * 150)

        return {
            "custom_id": "classification",
            "params": {
                "model": resolve_model(self.model),
                "max_tokens": scaled_max_tokens,
                "temperature": 0,
                "system": [
                    {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
                ],
                "messages": [{"role": "user", "content": prompt_text}],
            },
        }

    def parse_batch_result(self, raw_text, email_batch):
        """Parse a raw text response (from Batches API) into validated results.

        Same parsing/validation pipeline as _analyze_via_api, but without
        making the API call — the caller already has the response text.
        """
        if not raw_text:
            return None

        actions = self._extract_json_from_text(raw_text)
        if actions is None:
            logger.warning("  Batch result parse failed")
            logger.debug(f"  Response ({len(raw_text)} chars): {raw_text[:500]}")
            return None

        valid, invalid_indices = _validate_results(actions)
        if invalid_indices:
            logger.warning(f"  Dropped {len(invalid_indices)} malformed entries: indices {invalid_indices}")

        if valid:
            valid = _merge_known_fields(valid, email_batch)
        return valid if valid else None

    def analyze_batch(self, email_batch):
        """Send a batch of emails to Claude, return list of action item dicts.

        Routes to the API or CLI backend based on self.backend.
        Merges known fields (from_name, subject, date) back into results
        so Claude doesn't need to echo them.
        """
        if self.backend == "cli":
            results = self._analyze_via_cli(email_batch)
        else:
            results = self._analyze_via_api(email_batch)

        if results is not None:
            results = _merge_known_fields(results, email_batch)
        return results

    # ------------------------------------------------------------------
    # API backend
    # ------------------------------------------------------------------

    def _analyze_via_api(self, email_batch):
        """Analyze emails via the Anthropic Messages API."""
        from .api_client import call_claude, resolve_model

        prompt_text = self._build_prompt(email_batch)
        system = self._build_system_prompt()

        # Scale max_tokens to batch size (≈150 tokens per email + overhead)
        scaled_max_tokens = min(2048, 256 + len(email_batch) * 150)

        try:
            raw_output = call_claude(
                prompt=prompt_text,
                system_prompt=system,
                model=resolve_model(self.model),
                max_tokens=scaled_max_tokens,
                timeout=self.timeout,
                api_key=self.api_key,
                temperature=0,
                cache_system_prompt=True,
            )
        except Exception as e:
            logger.error(f"  Anthropic API call failed: {e}")
            return None

        if not raw_output:
            logger.warning("  Empty response from Anthropic API")
            return None

        actions = self._extract_json_from_text(raw_output)

        # Single retry on parse failure
        if actions is None:
            logger.warning("  Initial parse failed, retrying with repair prompt")
            actions = self._retry_parse(raw_output)

        if actions is None:
            logger.error("  Retry parse also failed — batch lost")
            logger.debug(f"  Response ({len(raw_output)} chars): {raw_output[:500]}")
            return None

        # Validate individual entries; drop malformed ones instead of losing the batch
        valid, invalid_indices = _validate_results(actions)
        if invalid_indices:
            logger.warning(f"  Dropped {len(invalid_indices)} malformed entries: indices {invalid_indices}")
        return valid if valid else None

    def _retry_parse(self, original_response):
        """Make one retry call with a tight prompt when the initial parse fails."""
        from .api_client import call_claude, resolve_model

        retry_prompt = (
            "Your previous response could not be parsed as valid JSON. "
            "Here is what you returned (truncated):\n\n"
            f"{original_response[:500]}\n\n"
            "Please return ONLY the JSON object with the email_actions array. "
            "No markdown fences. No explanation. No preamble. "
            "Start with { and end with }."
        )

        try:
            raw_output = call_claude(
                prompt=retry_prompt,
                system_prompt="You are a JSON repair assistant. Return only valid JSON.",
                model=resolve_model(self.model),
                max_tokens=2048,
                timeout=self.timeout,
                api_key=self.api_key,
                temperature=0,
            )
        except Exception as e:
            logger.error(f"  Retry API call failed: {e}")
            return None

        return self._extract_json_from_text(raw_output)

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

    # ------------------------------------------------------------------
    # Enriched classification (scorer-based pipeline)
    # ------------------------------------------------------------------

    def _build_enriched_prompt(self, enrichment_records):
        """Format enrichment records into a structured prompt for Haiku."""
        parts = [
            "Classify the following emails using the enrichment data provided.\n"
        ]

        for i, rec in enumerate(enrichment_records, 1):
            prob = rec["calibrated_probability"]
            tier = rec["confidence_tier"]
            sb = rec["sender_briefing"]
            tb = rec["thread_briefing"]

            parts.append(f"=== EMAIL {i} (score: {prob:.0%} {tier}) ===")
            parts.append(f"SENDER: {sb['summary']}")
            parts.append(f"SCORE: {rec['score_explanation']}")

            if rec["feature_checks"]:
                parts.append("FEATURE CHECKS:")
                for check in rec["feature_checks"]:
                    parts.append(f"- {check}")

            flags = rec.get("anomaly_flags", [])
            parts.append(f"ANOMALIES: {'; '.join(flags) if flags else 'None'}")

            tp = rec.get("time_pressure")
            parts.append(f"TIME PRESSURE: {tp if tp else 'None'}")

            parts.append(f"ARCHETYPE: {rec.get('archetype_prediction', 'standard_reply')}")
            parts.append(f"THREAD: {tb['summary']}")

            # Messages
            parts.append("--- MESSAGES ---")
            msgs = rec.get("messages", {})
            inbound = msgs.get("inbound")
            if inbound:
                parts.append(f"[Inbound] From: {inbound['sender']}")
                parts.append(inbound.get("body", "")[:2000])

            user_last = msgs.get("user_last")
            if user_last:
                parts.append(f"\n[User's last reply] {user_last.get('received_time', '')}")
                parts.append(user_last.get("body", "")[:1000])

            opener = msgs.get("thread_opener")
            if opener:
                parts.append(f"\n[Thread opener] From: {opener['sender']}")
                parts.append(opener.get("body", "")[:500])

            parts.append("")

        parts.append("=== END ===")
        return "\n".join(parts)

    def build_enriched_batch_params(self, enrichment_records, custom_id="classification"):
        """Build a Batches API request dict for enriched classification.

        Args:
            enrichment_records: list of enrichment dicts from assemble_enrichment().
            custom_id: identifier for this request in the batch.

        Returns:
            dict with 'custom_id' and 'params' keys.
        """
        from .api_client import resolve_model

        prompt_text = self._build_enriched_prompt(enrichment_records)

        system_text = (
            "You must return ONLY a valid JSON object matching this exact schema:\n\n"
            + json.dumps(ENRICHED_JSON_SCHEMA, indent=2)
            + "\n\nOutput ONLY the raw JSON object. "
            "No markdown fences. No preamble. No explanation. "
            "Start your response with { and end with }.\n\n"
            + ENRICHED_ANALYSIS_PROMPT
        )

        scaled_max_tokens = min(2048, 256 + len(enrichment_records) * 150)

        return {
            "custom_id": custom_id,
            "params": {
                "model": resolve_model(self.model),
                "max_tokens": scaled_max_tokens,
                "temperature": 0,
                "system": [
                    {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
                ],
                "messages": [{"role": "user", "content": prompt_text}],
            },
        }

    def parse_enriched_batch_result(self, raw_text, enrichment_records):
        """Parse a batch response and map results back to email IDs.

        Uses the same JSON extraction as parse_batch_result, but maps
        email_index back to the original email_id from enrichment records.
        Validates enriched schema fields (reason, archetype, confidence).
        """
        if not raw_text:
            return None

        actions = self._extract_json_from_text(raw_text)
        if actions is None:
            logger.warning("  Enriched batch result parse failed")
            return None

        valid, invalid_indices = _validate_enriched_results(actions)
        if invalid_indices:
            logger.warning(f"  Dropped {len(invalid_indices)} malformed entries: indices {invalid_indices}")

        if not valid:
            return None

        # Map email_index (1-based) back to email_id + merge metadata
        for result in valid:
            idx = result.get("email_index", 0) - 1
            if 0 <= idx < len(enrichment_records):
                rec = enrichment_records[idx]
                result["_email_id"] = rec["email_id"]
                # Merge sender/subject from the enrichment record messages
                inbound = rec.get("messages", {}).get("inbound", {})
                result.setdefault("from_name", inbound.get("sender", ""))
                result.setdefault("date", str(inbound.get("received_time", ""))[:10])

        return valid
