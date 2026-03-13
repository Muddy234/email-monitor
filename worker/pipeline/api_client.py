"""Anthropic API client — sync calls, prompt caching, and Message Batches.

Provides wrappers around the Anthropic Messages API and Batches API.
Each call instantiates its own client to avoid sharing API keys across
workers in the multi-user Phase 2 deployment.
"""

import logging
import os
import time

import anthropic

logger = logging.getLogger("clarion")

# Short names used in config → full Anthropic model identifiers.
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


def _extract_usage(message) -> dict:
    """Extract token usage from an Anthropic API response."""
    usage = getattr(message, "usage", None)
    if not usage:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def call_claude(
    prompt: str,
    system_prompt: str = "",
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 8192,
    timeout: int = 120,
    api_key: str = None,
    temperature: float = None,
    cache_system_prompt: bool = False,
) -> tuple:
    """Send a message to Claude and return (text, usage_dict).

    Args:
        prompt: The user message content.
        system_prompt: System instructions (str or list of content blocks).
        model: Model identifier (short name or full ID).
        max_tokens: Maximum response tokens.
        timeout: Request timeout in seconds.
        api_key: Anthropic API key (uses ANTHROPIC_API_KEY env var if None).
        temperature: Sampling temperature (0 = deterministic, None = API default).
        cache_system_prompt: If True, wrap system prompt with cache_control
            for Anthropic prompt caching (90% input token discount on cache hit).

    Returns:
        tuple: (response_text, usage_dict) where usage_dict has keys:
            input_tokens, output_tokens, cache_read_input_tokens,
            cache_creation_input_tokens.
    """
    resolved = resolve_model(model)
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

    # Build system parameter — supports caching and pre-built content blocks
    if isinstance(system_prompt, list):
        system = system_prompt
    elif cache_system_prompt and system_prompt:
        system = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
    else:
        system = system_prompt

    kwargs = {
        "model": resolved,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    message = client.messages.create(**kwargs)

    text = "".join(
        block.text for block in message.content if block.type == "text"
    )
    return text, _extract_usage(message)


# ---------------------------------------------------------------------------
# Message Batches API
# ---------------------------------------------------------------------------

def create_message_batch(requests, api_key=None, timeout=120):
    """Submit a list of message requests as an async batch (50% discount).

    Args:
        requests: List of dicts, each with 'custom_id' and 'params'.
            params follows the same schema as messages.create().
        api_key: Anthropic API key.
        timeout: HTTP timeout for the submission call.

    Returns:
        Batch object with .id and .processing_status.
    """
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    return client.messages.batches.create(requests=requests)


def poll_batch_until_done(batch_id, api_key=None, timeout=120,
                          poll_interval=3, max_wait=900):
    """Poll a batch until processing_status == 'ended'.

    Args:
        batch_id: The batch ID from create_message_batch().
        poll_interval: Seconds between status checks.
        max_wait: Maximum seconds to wait before raising TimeoutError.

    Returns:
        The completed Batch object.
    """
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    deadline = time.time() + max_wait

    while time.time() < deadline:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        time.sleep(poll_interval)

    raise TimeoutError(f"Batch {batch_id} did not complete within {max_wait}s")


def get_batch_results(batch_id, api_key=None, timeout=120):
    """Retrieve results for a completed batch.

    Returns:
        tuple: (results_dict, total_usage_dict)
            results_dict maps custom_id → response text (or None on failure).
            total_usage_dict aggregates tokens across all succeeded requests.
    """
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    results = {}
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            text = "".join(
                block.text for block in result.result.message.content
                if block.type == "text"
            )
            results[result.custom_id] = text
            usage = _extract_usage(result.result.message)
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)
        else:
            logger.warning(
                f"  Batch request {result.custom_id} failed: {result.result.type}"
            )
            results[result.custom_id] = None

    return results, total_usage


DIRECT_API_THRESHOLD = int(os.environ.get("DIRECT_API_THRESHOLD", "2"))


def _submit_direct(requests, api_key=None, timeout=120):
    """Execute requests synchronously via messages.create() for small batches.

    Avoids Batch API overhead (submission + polling) when there are only a
    few requests. Returns the same (results_dict, total_usage_dict) format.
    """
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    results = {}
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    for req in requests:
        custom_id = req["custom_id"]
        params = req["params"]
        try:
            message = client.messages.create(**params)
            text = "".join(
                block.text for block in message.content if block.type == "text"
            )
            results[custom_id] = text
            usage = _extract_usage(message)
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)
        except Exception as e:
            logger.warning(f"  Direct API call failed for {custom_id}: {e}")
            results[custom_id] = None

    succeeded = sum(1 for v in results.values() if v is not None)
    logger.info(f"  Direct API: {succeeded}/{len(requests)} succeeded")
    return results, total_usage


def submit_and_wait(requests, api_key=None, timeout=120,
                    poll_interval=3, max_wait=900):
    """Submit requests and block until results are ready.

    For small batches (≤DIRECT_API_THRESHOLD), uses synchronous API calls
    to avoid Batch API overhead. For larger batches, uses the async Batch
    API with 50% cost discount.

    Returns:
        tuple: (results_dict, total_usage_dict)
            results_dict maps custom_id → response text (or None on failure).
            total_usage_dict aggregates tokens across all succeeded requests.
    """
    if not requests:
        return {}, {}

    if len(requests) <= DIRECT_API_THRESHOLD:
        logger.info(f"  Small batch ({len(requests)} requests) — using direct API")
        return _submit_direct(requests, api_key=api_key, timeout=timeout)

    batch = create_message_batch(requests, api_key=api_key, timeout=timeout)
    logger.info(f"  Batch {batch.id} submitted ({len(requests)} requests)")

    poll_batch_until_done(
        batch.id, api_key=api_key, timeout=timeout,
        poll_interval=poll_interval, max_wait=max_wait,
    )

    results, total_usage = get_batch_results(batch.id, api_key=api_key, timeout=timeout)
    succeeded = sum(1 for v in results.values() if v is not None)
    logger.info(f"  Batch {batch.id} complete: {succeeded}/{len(requests)} succeeded")
    return results, total_usage
