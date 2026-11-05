"""Normalise one API response's usage block into the four numbers the controller needs.

Providers name them differently; the controller only wants prompt / cached / written /
completion tokens. Works on dicts and attribute objects (litellm Usage, harbor UsageInfo).

    >>> normalize_usage({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800},
    ...                  "completion_tokens": 50}).cached
    800.0
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    prompt: float          # full prompt size as the API counted it (cached + uncached)
    cached: float = 0.0    # cache-read tokens
    written: float = 0.0   # cache-write tokens (0 when the API does not report writes)
    completion: float = 0.0


def _get(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict):
            if n in obj and obj[n] is not None:
                return obj[n]
        elif getattr(obj, n, None) is not None:
            return getattr(obj, n)
    return default


def normalize_usage(u) -> Usage:
    """Anthropic (input_tokens + cache_read_input_tokens + cache_creation_input_tokens),
    OpenAI / vLLM (prompt_tokens, prompt_tokens_details.cached_tokens), harbor UsageInfo
    (prompt_tokens, cache_tokens, cache_creation_tokens)."""
    if u is None:
        return Usage(0.0)
    details = _get(u, "prompt_tokens_details")
    cached = float(_get(u, "cache_tokens", "cache_read_input_tokens", default=0) or 0)
    if not cached and details is not None:
        cached = float(_get(details, "cached_tokens", default=0) or 0)
    written = float(_get(u, "cache_creation_tokens", "cache_creation_input_tokens", default=0) or 0)
    if not written and details is not None:
        written = float(_get(details, "cache_write_tokens", default=0) or 0)
    prompt = _get(u, "prompt_tokens")
    if prompt is None:
        # Anthropic: input_tokens excludes the cached and the newly written part
        prompt = float(_get(u, "input_tokens", default=0) or 0) + cached + written
    completion = float(_get(u, "completion_tokens", "output_tokens", default=0) or 0)
    return Usage(float(prompt), cached, written, completion)
