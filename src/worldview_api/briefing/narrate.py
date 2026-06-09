"""Conversational spoken-word narration for the top-stories briefing.

The briefing reads the top clusters aloud via browser TTS. Reading raw cluster
`title`/`summary` verbatim sounds robotic — feed-derived text (e.g. an NWS alert
"SVRTOP ... * Until 115 AM CDT ...") is full of codes, markup, and literal
timestamps that get spelled out letter-for-letter. We rewrite the whole briefing
in one LLM call into a short, natural, professional script, and fall back to a
cleaned-up no-LLM version when the LLM is unavailable / over budget / slow.

Provider is the same config-driven, OpenAI-compatible client the cluster
summarizer uses. The output is keyed by `cluster_id` so the caller can fly the
globe to each story; we reconcile the model's response against the requested ids
so a partial/garbled response still yields a complete, correctly-ordered script.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Sequence, TypedDict

import openai
from pydantic import BaseModel, Field, ValidationError

from ..ask.places import country_name
from ..config import settings
from ..llm import get_client as _get_client
from ..llm import retry_after_seconds as _retry_after_seconds
from .budget import budget

log = logging.getLogger(__name__)

# 429 handling. The free-tier Gemini key is shared with the summarizer (20 RPM
# free-tier cap); a briefing landing during a summarizer burst gets a 429 whose
# retry window is short (~2s). We honor the server's retry hint, capped so an
# interactive briefing never stalls, and retry a couple of times before giving
# up to the cleaned-up fallback.
_RATE_LIMIT_BACKOFF_S = 2.5
_RATE_LIMIT_MAX_WAIT_S = 4.0
_RATE_LIMIT_RETRIES = 2


class BriefingInput(TypedDict):
    """One selected story, as handed to the narrator."""

    cluster_id: str
    title: str
    summary: str | None
    city: str | None
    country_code: str | None


# --------------------------------------------------------------------------- #
# Text cleaning (used for the no-LLM fallback and to tidy LLM inputs)
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
# Leading wire/product code token, e.g. "SVRTOP", "SVRTOP123" — 4+ caps at start.
_LEADING_CODE = re.compile(r"^[A-Z]{4,}\d*\b[\s:.-]*")
# Runs of dots / unicode ellipsis used as separators in feed text.
_ELLIPSIS = re.compile(r"\s*(?:\.{2,}|…)+\s*")
# Asterisk bullets the NWS uses to delimit fields.
_BULLET = re.compile(r"\s*\*+\s*")
_FIRST_SENTENCE = re.compile(r"^.{20,}?[.!?](?=\s|$)")


def clean_for_speech(text: str | None, max_chars: int = 220) -> str:
    """Strip feed codes / markup / separator runs and return a speakable phrase.

    Best-effort and conservative — it makes raw feed text readable for the
    fallback path; the LLM path does the genuinely conversational rewrite.
    """
    if not text:
        return ""
    t = _WS.sub(" ", text).strip()
    t = _LEADING_CODE.sub("", t)
    t = _ELLIPSIS.sub(", ", t)
    t = _BULLET.sub(" ", t)
    t = _WS.sub(" ", t).strip(" ,.")
    if not t:
        return ""
    m = _FIRST_SENTENCE.match(t)
    sentence = m.group(0) if m else t[:max_chars]
    if len(sentence) > max_chars:
        sentence = sentence[:max_chars].rstrip() + "…"
    return sentence


# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #


class _NarrationStory(BaseModel):
    cluster_id: str
    narration: str = Field("", max_length=600)


class _BriefingScript(BaseModel):
    intro: str = Field("", max_length=300)
    stories: list[_NarrationStory] = Field(default_factory=list)
    outro: str = Field("", max_length=300)


def _parse_script(content: str) -> _BriefingScript | None:
    """Validate the model output, tolerant of wrapping prose / fences."""
    content = (content or "").strip()
    try:
        return _BriefingScript.model_validate_json(content)
    except ValidationError:
        pass
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return _BriefingScript.model_validate_json(content[start : end + 1])
        except ValidationError:
            return None
    return None


SYSTEM_PROMPT = """You are JARVIS, delivering a short spoken news briefing out \
loud. You are given the top stories of the hour, each with an id, a location, a \
headline, and a summary. Rewrite them as a script that will be read aloud by a \
text-to-speech voice.

Tone: natural and professional, like a calm news anchor catching someone up. \
Conversational and fluent — never robotic. No opinion, no clickbait, no \
adjectives like "shocking", "tragic", or "huge". Just tell people what is \
happening, clearly.

For each story, write ONE to TWO short sentences (about 10-15 seconds of speech):
- Lead naturally with the place when it helps ("In southeastern Kansas, ...").
- Say what is happening in plain spoken language.
- NEVER read codes, identifiers, asterisks, or markup aloud (e.g. drop \
"SVRTOP", "*", "..."). NEVER read exact clock times or timestamps literally \
("12:37AM CDT", "Until 115 AM CDT") — say "tonight", "through early morning", \
or omit them.
- Use ONLY facts present in the provided headline/summary. Do not invent names, \
numbers, places, or details.

Also write:
- intro: one short opening line, e.g. "Here are the top stories at this hour."
- outro: one short closing line, e.g. "That's your briefing."

Output format — respond with ONLY a single JSON object and nothing else (no \
markdown fences, no commentary). Include exactly one entry per story id you were \
given, in the SAME order, reusing the given id verbatim:
{"intro": "<opening line>", "stories": [{"cluster_id": "<id>", "narration": \
"<1-2 sentence spoken narration>"}], "outro": "<closing line>"}
"""


# --------------------------------------------------------------------------- #
# Fallback (no-LLM) script
# --------------------------------------------------------------------------- #


def _location_label(story: BriefingInput) -> str | None:
    city = story.get("city")
    if city:
        return city
    cc = story.get("country_code")
    if cc:
        return country_name(cc) or cc
    return None


def _ensure_period(s: str) -> str:
    s = s.strip()
    if s and s[-1] not in ".!?":
        s += "."
    return s


def _fallback_narration(story: BriefingInput) -> str:
    where = _location_label(story)
    title = clean_for_speech(story.get("title"), max_chars=160)
    body = clean_for_speech(story.get("summary"))
    parts: list[str] = []
    if where:
        parts.append(f"In {where}.")
    if title:
        parts.append(_ensure_period(title))
    # Avoid repeating the headline when the summary just restates it.
    if body and body.lower() != title.lower():
        parts.append(_ensure_period(body))
    return " ".join(parts).strip()


def _default_intro(n: int) -> str:
    if n == 1:
        return "Here is the top story at this hour."
    return f"Here are the top {n} stories at this hour."


_DEFAULT_OUTRO = "That's your briefing."


class BriefingScriptOut(TypedDict):
    intro: str
    stories: list[dict]  # [{"cluster_id": str, "narration": str}]
    outro: str


def _fallback_script(stories: Sequence[BriefingInput]) -> BriefingScriptOut:
    return {
        "intro": _default_intro(len(stories)),
        "stories": [
            {"cluster_id": s["cluster_id"], "narration": _fallback_narration(s)}
            for s in stories
        ],
        "outro": _DEFAULT_OUTRO,
    }


# --------------------------------------------------------------------------- #
# LLM path
# --------------------------------------------------------------------------- #


def _format_user_prompt(stories: Sequence[BriefingInput]) -> str:
    lines = [f"There are {len(stories)} stories. Narrate each one.", ""]
    for i, s in enumerate(stories, start=1):
        where = _location_label(s) or "unknown location"
        lines.append(f"Story {i} — id: {s['cluster_id']}")
        lines.append(f"  Location: {where}")
        lines.append(f"  Headline: {clean_for_speech(s.get('title'), max_chars=200)}")
        summary = clean_for_speech(s.get("summary"), max_chars=400)
        if summary:
            lines.append(f"  Summary: {summary}")
        lines.append("")
    return "\n".join(lines)


def _call_llm(stories: Sequence[BriefingInput]) -> str | None:
    """One LLM call for the whole briefing. Returns raw content or None."""
    try:
        client = _get_client()
    except RuntimeError:  # LLM_API_KEY missing
        return None

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _format_user_prompt(stories)},
    ]

    def _call(strict: bool):
        kwargs: dict = dict(
            model=settings.briefing_llm_model or settings.llm_model,
            messages=messages,
            temperature=0.4,
            max_tokens=900,
            stream=False,
            timeout=settings.briefing_llm_timeout_s,
        )
        if strict:
            kwargs["response_format"] = {"type": "json_object"}
            base = (settings.llm_base_url or "").lower()
            if "nvidia" in base or "deepseek" in base:
                kwargs["extra_body"] = {"chat_template_kwargs": {"thinking": False}}
        return client.chat.completions.create(**kwargs)

    def _attempt():
        try:
            return _call(strict=True)
        except openai.BadRequestError:
            return _call(strict=False)

    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        try:
            resp = _attempt()
            return (resp.choices[0].message.content or "") if resp.choices else ""
        except openai.RateLimitError as e:
            if attempt >= _RATE_LIMIT_RETRIES:
                log.info("briefing: rate-limited after %d retries — degrading", attempt)
                return None
            wait = min(_retry_after_seconds(e) or _RATE_LIMIT_BACKOFF_S, _RATE_LIMIT_MAX_WAIT_S)
            log.info("briefing: 429 rate-limited, backing off %.1fs (retry %d)", wait, attempt + 1)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001 — any LLM failure degrades, never 5xx
            log.info("briefing: LLM call failed (%s) — degrading", type(e).__name__)
            return None
    return None


def _reconcile(
    parsed: _BriefingScript, stories: Sequence[BriefingInput]
) -> BriefingScriptOut:
    """Match the model's narrations back to the requested stories, in order.

    Missing / blank narrations (model dropped or mangled an id) are filled from
    the cleaned-up fallback; extra/invented ids are ignored.
    """
    by_id = {s.cluster_id: (s.narration or "").strip() for s in parsed.stories}
    out_stories: list[dict] = []
    for s in stories:
        narration = by_id.get(s["cluster_id"], "")
        if not narration:
            narration = _fallback_narration(s)
        out_stories.append({"cluster_id": s["cluster_id"], "narration": narration})
    intro = (parsed.intro or "").strip() or _default_intro(len(stories))
    outro = (parsed.outro or "").strip() or _DEFAULT_OUTRO
    return {"intro": intro, "stories": out_stories, "outro": outro}


def generate_briefing(
    stories: Sequence[BriefingInput],
) -> tuple[BriefingScriptOut, str]:
    """Produce the briefing script. Returns (script, source) where source is
    "llm" or "fallback". Never raises — always returns a playable script."""
    if not stories:
        return {"intro": "", "stories": [], "outro": ""}, "fallback"
    if not settings.llm_api_key:
        return _fallback_script(stories), "fallback"
    if not budget.try_acquire():
        log.info("briefing: LLM budget spent — degrading")
        return _fallback_script(stories), "fallback"

    content = _call_llm(stories)
    if content is None:
        return _fallback_script(stories), "fallback"
    parsed = _parse_script(content)
    if parsed is None:
        log.warning("briefing: could not parse LLM response: %.200s", content)
        return _fallback_script(stories), "fallback"
    return _reconcile(parsed, stories), "llm"
