"""Raw event list endpoints (recent + viewport)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from ..db import get_pool
from ..schemas import EventOut

router = APIRouter()


def _row_to_event(r: tuple) -> EventOut:
    cats = list(r[9]) if r[9] else []
    return EventOut(
        id=r[0],
        title=r[1],
        summary=r[2],
        url=r[3],
        source_outlet=r[4],
        occurred_at=r[5],
        lat=r[6],
        lon=r[7],
        country_code=r[8],
        categories=cats,
        importance=r[10],
        breaking="breaking" in cats,
        image_url=r[11] if len(r) > 11 else None,
        city=r[12] if len(r) > 12 else None,
        geo_precision=r[13] if len(r) > 13 else None,
    )


@router.get("/events/recent", response_model=list[EventOut])
def events_recent(
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(500, ge=1, le=5000),
    min_importance: float = Query(0.0, ge=0.0, le=1.0),
) -> list[EventOut]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary, url, source_outlet, occurred_at,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   country_code, categories, importance, image_url, city, geo_precision
            FROM events
            WHERE occurred_at >= %s
              AND coalesce(importance, 0) >= %s
            ORDER BY coalesce(importance, 0) DESC, occurred_at DESC
            LIMIT %s
            """,
            (since, min_importance, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


@router.get("/events", response_model=list[EventOut])
def events_in_viewport(
    south: float = Query(..., ge=-90, le=90),
    west: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(2000, ge=1, le=10000),
) -> list[EventOut]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary, url, source_outlet, occurred_at,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   country_code, categories, importance, image_url, city, geo_precision
            FROM events
            WHERE occurred_at >= %s
              AND ST_Intersects(
                    location::geometry,
                    ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                  )
            ORDER BY coalesce(importance, 0) DESC, occurred_at DESC
            LIMIT %s
            """,
            (since, west, south, east, north, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]
