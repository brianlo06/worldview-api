"""Top-stories spoken briefing endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Response

from ..config import settings
from ..db import get_pool
from ..schemas import BriefingResponse, BriefingStoryOut

router = APIRouter()


@router.post("/briefing", response_model=BriefingResponse)
def briefing(response: Response) -> BriefingResponse:
    """Top-stories briefing as a short, conversational spoken-word script.

    Selects the top N clusters (last 24h, >=2 events, by importance — the same
    selection the client used to do) and rewrites them into natural narration
    via the LLM, degrading to cleaned-up cluster text on any LLM/budget/timeout
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
            (since, n),
        )
        rows = cur.fetchall()

    selected = [
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
    script, source = generate_briefing(inputs)
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
    return BriefingResponse(
        intro=script["intro"], stories=stories_out, outro=script["outro"], source=source
    )
