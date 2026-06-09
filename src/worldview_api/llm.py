"""Shared OpenAI-compatible LLM client utilities.

The cluster summarizer and briefing narrator talk to the same config-driven
provider (`llm_base_url` / `llm_model` / `llm_api_key`). This module holds the
pieces both need: the client factory and 429 retry-hint parsing. Pacing,
budgets, and retry policy stay with the callers — they differ per workload
(batch summarizer vs interactive briefing).
"""

from __future__ import annotations

import re

from openai import OpenAI

from .config import settings

_RETRY_IN_BODY = re.compile(r"retry in ([\d.]+)s")


def get_client() -> OpenAI:
    if not settings.llm_api_key:
        raise RuntimeError(
            "LLM_API_KEY is not set — add it to worldview-api/.env"
        )
    # max_retries=0: the SDK's built-in retries fire rapidly and bypass the
    # callers' own pacing/backoff (which protect the shared RPM budget), so
    # callers do their own paced retries on 429 instead.
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        max_retries=0,
    )


def retry_after_seconds(err: Exception) -> float | None:
    """Best-effort parse of how long to wait from a 429 — header first, then the
    'Please retry in 2.23s' hint Gemini puts in the error body."""
    resp = getattr(err, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        val = headers.get("retry-after") or headers.get("Retry-After")
        try:
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            pass
    m = _RETRY_IN_BODY.search(str(getattr(err, "message", "") or err))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
