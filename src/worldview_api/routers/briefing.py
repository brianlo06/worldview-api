"""Top-stories spoken briefing endpoint."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Response

from ..config import settings
from ..db import get_pool
from ..schemas import BriefingResponse, BriefingStoryOut

log = logging.getLogger(__name__)

router = APIRouter()

# Last successful LLM-narrated briefing. When narration degrades (budget gate,
# 429, timeout), replaying a recent JARVIS script beats the robotic template
# fallback — the top stories barely move between 15-min ingest cycles.
_LAST_LLM_TTL_S = 20 * 60
_last_llm_briefing: tuple[float, BriefingResponse] | None = None

# Diversity cap, mirroring the frontend breaking list (hud/breaking.ts): NWS
# alert clusters dominate raw importance/event_count, so an uncapped top-5 is
# four thunderstorms and a flood. A briefing should cover the world, not one
# storm system. Backfilled from the overflow when there aren't enough
# distinct-category stories.
_MAX_STORIES_PER_CATEGORY = 2


def _diversify(rows: list[dict], n: int) -> list[dict]:
    picked: list[dict] = []
    overflow: list[dict] = []
    per_category: dict[str, int] = {}
    for s in rows:
        cat = s["category"] or "uncategorized"
        if per_category.get(cat, 0) >= _MAX_STORIES_PER_CATEGORY:
            overflow.append(s)
            continue
        per_category[cat] = per_category.get(cat, 0) + 1
        picked.append(s)
        if len(picked) >= n:
            return picked
    # Quiet news window: not enough category variety — fill from overflow so
    # the briefing still reaches its story count.
    picked.extend(overflow[: n - len(picked)])
    return picked


@router.post("/briefing", response_model=BriefingResponse)
def briefing(response: Response) -> BriefingResponse:
    """Top-stories briefing as a short, conversational spoken-word script.

    Selects the top N clusters (last 24h, >=2 events, by importance, capped
    per category for variety) and rewrites them into natural narration via the
    LLM, degrading to cleaned-up cluster text on any LLM/budget/timeout
    condition. Never 5xx for LLM reasons; an empty selection skips the LLM."""
    from ..briefing.narrate import BriefingInput, generate_briefing

    response.headers["Cache-Control"] = "no-store"
    n = settings.briefing_story_count
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   e.title,
                   e.summary,
                   e.url,
                   e.image_url,
                   e.source_outlet,
                   c.last_seen,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code,
                   e.city,
                   c.primary_category
            FROM clusters c
            JOIN events e ON e.id = c.representative_event_id
            WHERE c.last_seen >= %s
              AND c.event_count >= 2
              AND e.location IS NOT NULL
            ORDER BY coalesce(c.importance_score, 0) DESC,
                     c.event_count DESC,
                     c.last_seen DESC
            LIMIT %s
            """,
            # Over-fetch so the per-category cap still has enough candidates
            # to fill the briefing with diverse stories.
            (since, max(n * 6, 30)),
        )
        rows = cur.fetchall()

    candidates = [
        {
            "id": r[0],
            "title": r[1],
            "summary": r[2],
            "url": r[3],
            "image_url": r[4],
            "source_outlet": r[5],
            "occurred_at": r[6],
            "lat": r[7],
            "lon": r[8],
            "country_code": r[9],
            "city": r[10],
            "category": r[11],
        }
        for r in rows
    ]
    # Title-quality gate: a representative titled "World" or "Top Stories" is
    # a scraped section page, not a story — junk input the narrator can only
    # waffle about. Real headlines have at least a few words.
    candidates = [c for c in candidates if len((c["title"] or "").split()) >= 3]
    selected = _diversify(candidates, n)

    if not selected:
        return BriefingResponse(intro="", stories=[], outro="", source="fallback")

    inputs: list[BriefingInput] = [
        {
            "cluster_id": str(s["id"]),
            "title": s["title"],
            "summary": s["summary"],
            "city": s["city"],
            "country_code": s["country_code"],
        }
        for s in selected
    ]
    global _last_llm_briefing
    script, source = generate_briefing(inputs)
    if source == "fallback" and _last_llm_briefing is not None:
        cached_at, cached = _last_llm_briefing
        if time.monotonic() - cached_at < _LAST_LLM_TTL_S:
            log.info("briefing: narration degraded — replaying cached LLM script")
            return cached
    narration_by_id = {st["cluster_id"]: st["narration"] for st in script["stories"]}

    stories_out = [
        BriefingStoryOut(
            cluster_id=s["id"],
            narration=narration_by_id.get(str(s["id"]), ""),
            title=s["title"],
            summary=s["summary"],
            url=s["url"],
            image_url=s["image_url"],
            source_outlet=s["source_outlet"],
            lat=s["lat"],
            lon=s["lon"],
            country_code=s["country_code"],
            city=s["city"],
            category=s["category"],
            occurred_at=s["occurred_at"],
        )
        for s in selected
    ]
    out = BriefingResponse(
        intro=script["intro"], stories=stories_out, outro=script["outro"], source=source
    )
    if source == "llm":
        _last_llm_briefing = (time.monotonic(), out)
    return out
