"""One-line JARVIS read of an anomaly — what's spiking and why.

Generated in the background at detection time (analyze/anomalies.py) and
stored on the anomaly row, so serving it costs nothing. LLM path is budget-
gated (analyze/budget.py) and degrades to a factual template line; like the
briefing, it never raises for LLM reasons.
"""

from __future__ import annotations

import logging

import openai

from ..config import settings
from ..llm import get_client
from ..regions import region_name
from .budget import budget

log = logging.getLogger(__name__)

MAX_SYNOPSIS_CHARS = 220

SYSTEM_PROMPT = """You are JARVIS, monitoring a global news feed. A region's \
event volume just spiked past its statistical baseline. Given the region, the \
spike multiplier, and the headlines driving it, write ONE short sentence \
(under 200 characters) telling your principal what's going on — composed, \
factual, conversational. Example: "News volume out of Russia is running five \
times normal, driven almost entirely by the new EU sanctions package."

Rules: no alarmist adjectives, no speculation beyond the headlines, never \
read codes or timestamps aloud, use ONLY facts present in the headlines. \
Respond with ONLY the sentence — no quotes, no preamble."""


def _template_synopsis(region: str, multiplier: float, titles: list[str]) -> str:
    base = f"Event volume out of {region} is {multiplier:.1f}× its normal rate"
    if titles:
        # Headlines can be long; keep the line speakable.
        top = titles[0][:90].rstrip()
        return f"{base} — top story: {top}."
    return f"{base}."


def generate_synopsis(
    region_code: str, multiplier: float, titles: list[str]
) -> str:
    """Always returns a synopsis line; the LLM is an upgrade, not a dependency."""
    region = region_name(region_code) or region_code
    fallback = _template_synopsis(region, multiplier, titles)
    if not settings.llm_api_key or not titles:
        return fallback
    if not budget.try_acquire():
        log.info("anomaly synopsis: LLM budget spent — using template")
        return fallback

    headlines = "\n".join(f"- {t[:160]}" for t in titles[:5])
    user = (
        f"Region: {region}\n"
        f"Event volume: {multiplier:.1f}x the normal rate\n"
        f"Driving headlines:\n{headlines}"
    )
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=(
                settings.anomaly_llm_model
                or settings.briefing_llm_model
                or settings.llm_model
            ),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=120,
            stream=False,
            timeout=settings.anomaly_llm_timeout_s,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        text = text.strip('"').strip()
        if not text:
            return fallback
        if len(text) > MAX_SYNOPSIS_CHARS:
            text = text[:MAX_SYNOPSIS_CHARS].rstrip() + "…"
        return text
    except openai.APIStatusError as e:
        log.info("anomaly synopsis: LLM %s — using template", e.status_code)
        return fallback
    except Exception as e:  # noqa: BLE001 — background path, never raise
        log.info("anomaly synopsis: %s — using template", type(e).__name__)
        return fallback
