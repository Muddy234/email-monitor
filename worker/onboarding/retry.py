"""Retry wrapper around call_claude for onboarding API calls.

Handles rate limits (429), timeouts, and transient server errors with
exponential backoff. Does not modify the existing call_claude function.
"""

import logging
import random
import time

import anthropic

from pipeline.api_client import call_claude, resolve_model

logger = logging.getLogger("worker.onboarding")


def call_with_retry(
    prompt,
    system_prompt="",
    model="haiku",
    max_tokens=4096,
    temperature=0,
    max_retries=3,
    timeout=120,
    cache_system_prompt=True,
    api_key=None,
):
    """Call Claude with exponential backoff retry on transient failures.

    Args:
        prompt: User message content.
        system_prompt: System instructions.
        model: Short model name or full ID.
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        max_retries: Maximum retry attempts.
        timeout: Per-call timeout in seconds.
        cache_system_prompt: Enable prompt caching.
        api_key: Anthropic API key (None → env var fallback).

    Returns:
        tuple: (response_text, usage_dict), or (None, {}) if all retries exhausted.
    """
    resolved = resolve_model(model)
    errors = []

    for attempt in range(max_retries + 1):
        try:
            return call_claude(
                prompt=prompt,
                system_prompt=system_prompt,
                model=resolved,
                max_tokens=max_tokens,
                timeout=timeout,
                temperature=temperature,
                cache_system_prompt=cache_system_prompt,
                api_key=api_key,
            )
        except anthropic.RateLimitError as e:
            errors.append("rate_limit")
            if attempt == max_retries:
                logger.error(f"Rate limit exceeded after {max_retries} retries (errors: {errors})")
                return None, {}
            retry_after = getattr(e, "retry_after", None)
            wait = float(retry_after) if retry_after else min(2 ** (attempt + 1), 60)
            wait += random.uniform(0, 1)
            logger.warning(f"Rate limited, waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
        except anthropic.APITimeoutError:
            errors.append("timeout")
            if attempt == max_retries:
                logger.error(f"Timeout after {max_retries} retries (errors: {errors})")
                return None, {}
            wait = min(2 ** (attempt + 1), 60) + random.uniform(0, 1)
            logger.warning(f"Timeout, retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                errors.append(f"server_{e.status_code}")
                if attempt == max_retries:
                    logger.error(f"Server error {e.status_code} after {max_retries} retries (errors: {errors})")
                    return None, {}
                wait = min(2 ** (attempt + 1), 60) + random.uniform(0, 1)
                logger.warning(f"Server error {e.status_code}, retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                logger.error(f"Non-retryable API error {e.status_code}: {e}")
                raise
