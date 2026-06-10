"""The POST /ask pipeline: normalize → cache → retrieve → synthesize/degrade.

Design constraints (see openspec change viral-share-loop/design.md):
  * Free-tier LLM, single 2 GB box, uncached pgvector search → the COMMON path
    must be a cache hit. Cold unique asks fall through to retrieval + one
    budgeted LLM call, and degrade to a templated (no-LLM) answer whenever the
    budget is spent or the model is slow. The endpoint never 5xx's on LLM.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence
from uuid import UUID

from ..config import settings
from ..db import get_pool
from .budget import budget
# regions is FIPS-aware (event country codes are GDELT FIPS); places keeps the
# question-text alias/intent maps.
from ..regions import region_name as country_name
from .places import COUNTRY_ALIASES, detect_country

log = logging.getLogger(__name__)

# Intent phrases that should collapse onto a single cache key regardless of
# exact wording, so the highest-volume general-public questions share an entry
# (and line up with the pre-baked set).
_BIGGEST_STORY_RE = re.compile(
    r"\b(biggest|top|main|headline)\b.*\b(story|stories|news)\b"
    r"|\bwhat'?s? (the )?(biggest|top|main) (story|news)\b"
    r"|\bwhat'?s? happening( in the world)?( right now| today)?\??$"
)
_WORLD_OK_RE = re.compile(r"\bis the world (ok|okay|alright|fine|safe)\b")
_NEAR_ME_RE = re.compile(r"\b(near|around|by|close to) me\b|\bmy (area|city|town|region)\b")
# Generic "give me the news for <country>" phrasing — only THIS collapses onto a
# country intent; specific questions that merely mention a country stay topical
# (semantic search) so the answer actually addresses them.
_GENERIC_COUNTRY_RE = re.compile(
    r"\b(what'?s?|whats) (happening|going on|new|the news)\b"
    r"|\b(any |the )?news\b|\bupdate(s)?\b|\bsituation\b"
)


@dataclass
class Story:
    """A unified 'thing happening' — a cluster or a raw event — used by the
    synthesis prompt, the degraded templated answer, and the results list the
    UI renders (so an ask/city view surfaces several stories, not just one)."""

    id: str | None
    title: str
    summary: str | None
    lat: float | None
    lon: float | None
    country_code: str | None
    city: str | None
    event_count: int
    importance: float | None
    image_url: str | None = None
    source_outlet: str | None = None


@dataclass
class AnswerResult:
    answer: str
    place: str | None = None
    fly_lat: float | None = None
    fly_lon: float | None = None
    cluster_refs: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)  # nearby/related stories for the UI list
    stats: dict = field(default_factory=dict)
    source: str = "live"  # 'live' | 'degraded' | 'prebaked' | 'cache'
    cacheable: bool = True


# --------------------------------------------------------------------------- #
# 2.1  Normalization → cache key
# --------------------------------------------------------------------------- #

def normalize_question(
    question: str,
    lat: float | None = None,
    lon: float | None = None,
) -> tuple[str, str]:
    """Return (normalized_key, intent).

    intent ∈ {'biggest_story', 'world_ok', 'near_me', 'topical'} drives which
    retrieval path runs. The key is what we look up / store in ask_cache;
    'near me' keys are bucketed by rounded coordinates so nearby users share an
    entry.
    """
    q = re.sub(r"\s+", " ", (question or "").strip().lower())
    q = q.strip(" ?.!")

    if _BIGGEST_STORY_RE.search(q):
        return "intent:biggest_story", "biggest_story"
    if _WORLD_OK_RE.search(q):
        return "intent:world_ok", "world_ok"
    if _NEAR_ME_RE.search(q) and lat is not None and lon is not None:
        d = max(0, settings.ask_geo_bucket_decimals)
        return f"intent:near_me:{round(lat, d)},{round(lon, d)}", "near_me"
    if lat is not None and lon is not None and not q:
        # Bare city view (no question) — treat as near_me.
        d = max(0, settings.ask_geo_bucket_decimals)
        return f"intent:near_me:{round(lat, d)},{round(lon, d)}", "near_me"
    # Generic "what's happening in <country>" / bare country name → country intent.
    if q in COUNTRY_ALIASES:
        return f"intent:country:{COUNTRY_ALIASES[q]}", "country"
    if _GENERIC_COUNTRY_RE.search(q):
        cc = detect_country(q)
        if cc:
            return f"intent:country:{cc}", "country"
    return f"topical:{q}", "topical"


# --------------------------------------------------------------------------- #
# 2.2  Cache lookup / store
# --------------------------------------------------------------------------- #

def cache_get(normalized_key: str) -> AnswerResult | None:
    """Return a cached answer if present and fresh. Pre-baked rows are always
    considered warm (refreshed each ingest cycle); other rows honor the TTL."""
    ttl = max(0, settings.ask_cache_ttl_seconds)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT answer, place, fly_lat, fly_lon, cluster_refs, stats, source,
                   (source = 'prebaked'
                    OR computed_at > NOW() - (%s * INTERVAL '1 second')) AS fresh
            FROM ask_cache
            WHERE normalized_key = %s
            """,
            (ttl, normalized_key),
        )
        row = cur.fetchone()
    if not row or not row[7]:
        return None
    raw_stats = row[5] if isinstance(row[5], dict) else {}
    # results are persisted inside the stats JSONB (under _results) to avoid a
    # separate column; pull them back out so cached/prebaked answers still carry
    # the story list the UI renders.
    results = raw_stats.pop("_results", []) or []
    return AnswerResult(
        answer=row[0],
        place=row[1],
        fly_lat=row[2],
        fly_lon=row[3],
        cluster_refs=[str(x) for x in (row[4] or [])],
        results=results,
        stats=raw_stats,
        source="cache" if row[6] != "prebaked" else "prebaked",
        cacheable=True,
    )


def cache_put(normalized_key: str, question: str, result: AnswerResult, source: str) -> None:
    if not result.cacheable:
        return
    refs: list[UUID] = []
    for r in result.cluster_refs:
        try:
            refs.append(UUID(r))
        except (ValueError, AttributeError):
            continue
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ask_cache
                (normalized_key, question, answer, place, fly_lat, fly_lon,
                 cluster_refs, stats, source, computed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (normalized_key) DO UPDATE SET
                question = EXCLUDED.question,
                answer = EXCLUDED.answer,
                place = EXCLUDED.place,
                fly_lat = EXCLUDED.fly_lat,
                fly_lon = EXCLUDED.fly_lon,
                cluster_refs = EXCLUDED.cluster_refs,
                stats = EXCLUDED.stats,
                source = EXCLUDED.source,
                computed_at = NOW()
            """,
            (
                normalized_key,
                question[:500] if question else None,
                result.answer,
                result.place,
                result.fly_lat,
                result.fly_lon,
                refs,
                _json({**(result.stats or {}), "_results": result.results}),
                source,
            ),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# 2.3 / 2.6  Retrieval
# --------------------------------------------------------------------------- #

def retrieve_clusters(query: str) -> list[Story]:
    """Semantic retrieval over cluster centroids (same space as the events that
    seeded them). Mirrors the /search endpoint but returns Story objects."""
    q = (query or "").strip()
    if not q:
        return []
    import numpy as np

    from ..embed.embed import embed_texts

    query_vec = np.asarray(embed_texts([q])[0], dtype=np.float32)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, e.title, e.summary,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code, e.city, c.event_count, c.importance_score,
                   e.image_url, e.source_outlet,
                   1 - (c.centroid_embedding <=> %s) AS similarity
            FROM clusters c
            JOIN events e ON e.id = c.representative_event_id
            WHERE c.last_seen > NOW() - (%s * INTERVAL '1 hour')
            ORDER BY c.centroid_embedding <=> %s
            LIMIT %s
            """,
            (query_vec, settings.ask_search_hours, query_vec, settings.ask_search_limit),
        )
        rows = cur.fetchall()

    stories: list[Story] = []
    for r in rows:
        if r[11] is not None and r[11] < settings.ask_min_similarity:
            continue
        stories.append(
            Story(
                id=str(r[0]), title=r[1] or "untitled", summary=r[2],
                lat=r[3], lon=r[4], country_code=r[5], city=r[6],
                event_count=r[7] or 1, importance=r[8],
                image_url=r[9], source_outlet=r[10],
            )
        )
    return stories


def retrieve_top_clusters(limit: int) -> list[Story]:
    """Highest-importance recent clusters — for 'biggest story' / 'world ok'
    intents and pre-baking. No embedding inference required."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, e.title, e.summary,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code, e.city, c.event_count, c.importance_score,
                   e.image_url, e.source_outlet
            FROM clusters c
            JOIN events e ON e.id = c.representative_event_id
            WHERE c.last_seen > NOW() - (%s * INTERVAL '1 hour')
              AND c.event_count >= 2
            ORDER BY COALESCE(c.importance_score, 0) DESC, c.event_count DESC
            LIMIT %s
            """,
            (settings.ask_search_hours, limit),
        )
        rows = cur.fetchall()
    return [
        Story(
            id=str(r[0]), title=r[1] or "untitled", summary=r[2], lat=r[3], lon=r[4],
            country_code=r[5], city=r[6], event_count=r[7] or 1, importance=r[8],
            image_url=r[9], source_outlet=r[10],
        )
        for r in rows
    ]


def retrieve_top_by_country(country_code: str, limit: int = 8) -> list[Story]:
    """Highest-importance recent clusters in one country — the 'country' intent
    and per-country pre-baking. No embedding inference."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, e.title, e.summary,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code, e.city, c.event_count, c.importance_score,
                   e.image_url, e.source_outlet
            FROM clusters c
            JOIN events e ON e.id = c.representative_event_id
            WHERE c.last_seen > NOW() - (%s * INTERVAL '1 hour')
              AND c.primary_country = %s
            ORDER BY COALESCE(c.importance_score, 0) DESC, c.event_count DESC
            LIMIT %s
            """,
            (settings.ask_search_hours, country_code, limit),
        )
        rows = cur.fetchall()
    return [
        Story(
            id=str(r[0]), title=r[1] or "untitled", summary=r[2], lat=r[3], lon=r[4],
            country_code=r[5], city=r[6], event_count=r[7] or 1, importance=r[8],
            image_url=r[9], source_outlet=r[10],
        )
        for r in rows
    ]


def retrieve_near(lat: float, lon: float, limit: int = 8, half_deg: float = 2.0) -> list[Story]:
    """Bbox retrieval over recent events around a point — the 'near me' path,
    no embedding inference (cheaper than semantic search for purely-local asks)."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   country_code, city, importance, image_url, source_outlet
            FROM events
            WHERE occurred_at > NOW() - (%s * INTERVAL '1 hour')
              AND ST_Intersects(
                    location::geometry,
                    ST_MakeEnvelope(%s, %s, %s, %s, 4326))
            ORDER BY COALESCE(importance, 0) DESC, occurred_at DESC
            LIMIT %s
            """,
            (
                settings.ask_search_hours,
                lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg,
                limit,
            ),
        )
        rows = cur.fetchall()
    return [
        Story(
            id=str(r[0]), title=r[1] or "untitled", summary=r[2], lat=r[3], lon=r[4],
            country_code=r[5], city=r[6], event_count=1, importance=r[7],
            image_url=r[8], source_outlet=r[9],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# 2.3  LLM synthesis (budget-gated)  +  2.5 degraded path
# --------------------------------------------------------------------------- #

_SYNTH_SYSTEM = """You are WORLDVIEW, a calm command-center intelligence \
assistant briefing a user about what is happening in the world right now. You \
are given the user's question and a short list of CURRENT real news stories \
retrieved for it. In 1-2 sentences, answer the question using ONLY those \
stories. Be factual and composed. Do not invent events, numbers, or places \
that are not in the provided stories. If the stories do not really answer the \
question, say so briefly. No preamble, no markdown — just the answer."""


def _synthesize(
    question: str, stories: Sequence[Story], use_budget: bool = True
) -> str | None:
    """One LLM call. Returns None on budget-exhaustion / any failure so the
    caller degrades. Never raises. `use_budget=False` is for pre-baking (batch,
    runs in the ingest cycle) so it doesn't draw down the interactive budget."""
    if not stories:
        return None
    if not settings.llm_api_key:
        return None
    if use_budget and not budget.try_acquire():
        log.info("ask: interactive LLM budget spent — degrading")
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            max_retries=0,
            timeout=settings.ask_llm_timeout_s,
        )
        lines = []
        for i, s in enumerate(stories[:6], start=1):
            where = s.city or s.country_code or "unknown location"
            body = (s.summary or "").strip()[:240]
            lines.append(f"{i}. [{where}] {s.title}. {body}".strip())
        user = f"Question: {question}\n\nCurrent stories:\n" + "\n".join(lines)
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=180,
            stream=False,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        return text or None
    except Exception as e:  # noqa: BLE001 — any LLM failure degrades, never 5xx
        log.info("ask: synthesis failed (%s) — degrading", type(e).__name__)
        return None


def _degraded_answer(question: str, stories: Sequence[Story]) -> str:
    """Templated answer from the top story's existing summary — no LLM."""
    if not stories:
        return "Nothing notable is breaking on that right now."
    top = stories[0]
    # Never surface a bare ISO code — prefer a city, then a mapped country
    # name, else omit the location lead entirely.
    where = top.city or country_name(top.country_code)
    # Colon keeps the body's own capitalization intact — reads cleanly whether
    # the body is a full sentence or a proper-noun headline.
    lead = f"In {where}: " if where else ""
    body = (top.summary or top.title or "").strip()
    if body and not body.endswith((".", "!", "?")):
        body += "."
    extra = ""
    n = len(stories) - 1
    if n == 1:
        extra = " 1 related development is also active."
    elif n > 1:
        extra = f" {n} related developments are also active."
    return (lead + body + extra).strip()


# --------------------------------------------------------------------------- #
# 2.4  Orchestration
# --------------------------------------------------------------------------- #

def _story_to_result(s: Story) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "summary": s.summary,
        "lat": s.lat,
        "lon": s.lon,
        "place": s.city or country_name(s.country_code),
        "source_outlet": s.source_outlet,
        "image_url": s.image_url,
        "country_code": s.country_code,
        "city": s.city,
    }


def _build_result(stories: Sequence[Story], answer: str, source: str) -> AnswerResult:
    if not stories:
        return AnswerResult(answer=answer, source=source, stats={"stories": 0})
    top = stories[0]
    # Never hand a bare ISO code to the frontend / share card.
    place = top.city or country_name(top.country_code)
    refs = [s.id for s in stories if s.id]
    return AnswerResult(
        answer=answer,
        place=place,
        fly_lat=top.lat,
        fly_lon=top.lon,
        cluster_refs=refs,
        results=[_story_to_result(s) for s in stories],
        stats={
            "stories": len(stories),
            "event_count": sum(s.event_count for s in stories),
            "sources": top.event_count,
        },
        source=source,
    )


def answer_question(
    question: str,
    lat: float | None = None,
    lon: float | None = None,
) -> AnswerResult:
    """Top-level entry for POST /ask. Always returns an AnswerResult; never
    raises for LLM/budget reasons."""
    key, intent = normalize_question(question, lat, lon)

    cached = cache_get(key)
    if cached is not None:
        return cached

    if intent == "near_me" and lat is not None and lon is not None:
        stories = retrieve_near(lat, lon)
    elif intent in ("biggest_story", "world_ok"):
        stories = retrieve_top_clusters(settings.ask_search_limit)
    elif intent == "country":
        cc = key.rsplit(":", 1)[-1]
        stories = retrieve_top_by_country(cc, settings.ask_search_limit)
    else:
        stories = retrieve_clusters(question)

    if not stories:
        # No fly-to, no error, and don't cache emptiness for long (cacheable
        # stays True but the TTL handles refresh as data arrives).
        result = AnswerResult(
            answer="Nothing notable is breaking on that right now.",
            source="live",
            stats={"stories": 0},
        )
        cache_put(key, question, result, source="live")
        return result

    synth = _synthesize(question, stories)
    if synth is not None:
        result = _build_result(stories, synth, source="live")
        cache_put(key, question, result, source="live")
        return result

    # Degraded path — templated, no LLM. Still cached so repeat asks are cheap.
    result = _build_result(stories, _degraded_answer(question, stories), source="degraded")
    cache_put(key, question, result, source="degraded")
    return result


def _json(obj) -> str:
    import json

    return json.dumps(obj, default=str)
