"""Anthropic API client — replaces Claude CLI subprocess calls.

Provides a thin wrapper around the Anthropic Messages API. Each call
instantiates its own client to avoid sharing API keys across Celery workers
in the multi-user Phase 2 deployment.
"""

import logging

import anthropic

logger = logging.getLogger("email_monitor")

# Short names used in config → full Anthropic model identifiers.
# NOTE: Verify these model IDs against current Anthropic API docs before
# hardcoding — model IDs change with new releases.
MODEL_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}


def resolve_model(short_name: str) -> str:
    """Map a short config name ('sonnet') to a full model ID.

    If *short_name* is already a full identifier (contains a '-'), it is
    returned as-is so callers can pass either form.
    """
    return MODEL_MAP.get(short_name, short_name)


def call_claude(
    prompt: str,
    system_prompt: str = "",
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 8192,
    timeout: int = 120,
    api_key: str = None,
) -> str:
    """Send a message to Claude and return the text response.

    A fresh client is instantiated per call. The Anthropic SDK is lightweight,
    and this avoids a concurrency bug: in Phase 2, Celery workers process
    requests for different users. A module-level singleton would silently share
    whichever API key was initialized first across all users in the same
    worker process. Per-call instantiation keeps keys correctly scoped.

    Args:
        prompt: The user message content.
        system_prompt: System instructions.
        model: Model identifier (short name or full ID).
        max_tokens: Maximum response tokens.
        timeout: Request timeout in seconds.
        api_key: Anthropic API key (uses ANTHROPIC_API_KEY env var if None).

    Returns:
        The assistant's response text.

    Raises:
        anthropic.APIError: On API failures (rate limit, auth, etc.).
        anthropic.APITimeoutError: If the request exceeds timeout.
    """
    resolved = resolve_model(model)
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

    message = client.messages.create(
        model=resolved,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract text from response content blocks
    return "".join(
        block.text for block in message.content if block.type == "text"
    )
