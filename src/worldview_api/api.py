from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .db import close_pool, get_pool


class AnomalyOut(BaseModel):
    id: UUID
    region_code: str
    started_at: datetime
    last_seen_at: datetime
    peak_rate: float
    baseline_rate: float
    sigma_above: float
    pulse_lat: float | None = None
    pulse_lon: float | None = None
    driver_titles: list[str]


class SearchRequest(BaseModel):
    query: str
    hours: int = 48
    limit: int = 30
    min_similarity: float = 0.45  # below this we treat the result as off-topic


class SearchResultOut(BaseModel):
    cluster_id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    event_count: int
    category: str | None = None
    importance: float | None = None
    similarity: float
    breaking: bool = False
    geo_precision: str | None = None


class ClusterOut(BaseModel):
    """A cluster, surfaced as its representative event (image, headline, URL),
    with cluster context (event_count) for the 'N sources' indicator."""
    id: UUID
    # Representative event fields — picked from the cluster member nearest to
    # the centroid that has an image (or just nearest if none do)
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    # Cluster context
    first_seen: datetime
    last_seen: datetime
    event_count: int
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    category: str | None = None
    importance: float | None = None
    breaking: bool = False
    geo_precision: str | None = None


class ClusterMemberOut(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    occurred_at: datetime
    categories: list[str]


class ClusterDetailOut(ClusterOut):
    members: list[ClusterMemberOut]


class MarketOut(BaseModel):
    symbol: str
    name: str
    city: str
    country_code: str | None = None
    lat: float
    lon: float
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    currency: str | None = None
    updated_at: datetime


class EventOut(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    occurred_at: datetime
    lat: float
    lon: float
    country_code: str | None = None
    city: str | None = None
    categories: list[str]
    importance: float | None = None
    breaking: bool = False
    geo_precision: str | None = None


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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_pool()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="worldview-api", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/anomalies", response_model=list[AnomalyOut])
def anomalies() -> list[AnomalyOut]:
    """Active regions whose recent event rate has spiked past baseline+3σ.

    Each anomaly carries up to 3 driver-cluster titles — the stories actually
    driving the spike — so the frontend can show 'why' without an extra fetch.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id,
                   a.region_code,
                   a.started_at,
                   a.last_seen_at,
                   a.peak_rate,
                   a.baseline_rate,
                   a.sigma_above,
                   a.pulse_lat,
                   a.pulse_lon,
                   coalesce(
                     (
                       SELECT array_agg(c.title ORDER BY c.event_count DESC)
                       FROM clusters c
                       WHERE c.id = ANY(a.driver_cluster_ids)
                     ),
                     '{}'::text[]
                   ) AS driver_titles
            FROM anomalies a
            WHERE a.status = 'active'
              AND a.last_seen_at > NOW() - INTERVAL '2 hours'
            ORDER BY a.sigma_above DESC
            """
        )
        rows = cur.fetchall()
    return [
        AnomalyOut(
            id=r[0],
            region_code=r[1],
            started_at=r[2],
            last_seen_at=r[3],
            peak_rate=r[4],
            baseline_rate=r[5],
            sigma_above=r[6],
            pulse_lat=r[7],
            pulse_lon=r[8],
            driver_titles=(list(r[9]) if r[9] else [])[:3],
        )
        for r in rows
    ]


@app.post("/search", response_model=list[SearchResultOut])
def search(body: SearchRequest) -> list[SearchResultOut]:
    """Embed the query and find nearest clusters by centroid similarity.

    Uses the same fastembed model that embedded the events, so query and
    cluster vectors live in the same space. Filters by `min_similarity`
    so off-topic queries return [] instead of bottom-rank noise.
    """
    q = body.query.strip()
    if not q:
        return []

    # Import lazily so the fastembed model only loads when /search is hit
    import numpy as np

    from .embed.embed import embed_texts

    # pgvector's <=> operator wants a vector-typed operand; np.ndarray adapts
    # correctly via the pgvector psycopg adapter (list[float] would adapt as
    # a double precision[] and the operator wouldn't match).
    query_vec = np.asarray(embed_texts([q])[0], dtype=np.float32)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id,
                   e.title,
                   e.summary,
                   e.url,
                   e.image_url,
                   e.source_outlet,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code,
                   e.city,
                   c.event_count,
                   c.primary_category,
                   c.importance_score,
                   1 - (c.centroid_embedding <=> %s) AS similarity,
                   e.geo_precision
            FROM clusters c
            LEFT JOIN LATERAL (
                SELECT *
                FROM events e2
                WHERE e2.cluster_id = c.id
                  AND e2.embedding IS NOT NULL
                ORDER BY
                    -- Prefer the member with the most precise location so the
                    -- cluster dot doesn't sit on a country centroid when a
                    -- sibling row knows the actual city.
                    CASE e2.geo_precision
                        WHEN 'point'   THEN 0
                        WHEN 'city'    THEN 1
                        WHEN 'state'   THEN 2
                        WHEN 'country' THEN 3
                        ELSE 4
                    END ASC,
                    (e2.image_url IS NOT NULL) DESC,
                    e2.embedding <=> c.centroid_embedding ASC
                LIMIT 1
            ) e ON true
            WHERE c.last_seen > NOW() - INTERVAL '{body.hours} hours'
              AND e.id IS NOT NULL
            ORDER BY c.centroid_embedding <=> %s
            LIMIT %s
            """,
            (query_vec, query_vec, body.limit),
        )
        rows = cur.fetchall()

    results: list[SearchResultOut] = []
    for r in rows:
        sim = r[13] if r[13] is not None else 0.0
        if sim < body.min_similarity:
            continue
        importance = r[12]
        event_count = r[10]
        breaking = event_count >= 10 or (importance is not None and importance >= 0.85)
        results.append(
            SearchResultOut(
                cluster_id=r[0],
                title=r[1] or "untitled",
                summary=r[2],
                url=r[3],
                image_url=r[4],
                source_outlet=r[5],
                lat=r[6],
                lon=r[7],
                country_code=r[8],
                city=r[9],
                event_count=event_count,
                category=r[11],
                importance=importance,
                similarity=sim,
                breaking=breaking,
                geo_precision=r[14],
            )
        )
    return results


@app.get("/clusters", response_model=list[ClusterOut])
def clusters(
    response: Response,
    hours: int = Query(48, ge=1, le=720),
    min_events: int = Query(1, ge=1, le=100),
    limit: int = Query(500, ge=1, le=5000),
) -> list[ClusterOut]:
    # Ingest runs every 15 min; 30s CDN cache lets a public deploy survive
    # bursts without each visit hammering Postgres.
    response.headers["Cache-Control"] = "public, max-age=30"
    """Active clusters surfaced as their representative event.

    For each cluster, pick the member that (a) has an image, and (b) is
    closest to the centroid embedding. The frontend renders that event's
    headline/image/URL, plus an `event_count` indicator showing how many
    other sources are behind the same story.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
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
                   c.first_seen,
                   c.last_seen,
                   c.event_count,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code,
                   e.city,
                   c.primary_category,
                   c.importance_score,
                   e.geo_precision
            FROM clusters c
            LEFT JOIN LATERAL (
                SELECT *
                FROM events e2
                WHERE e2.cluster_id = c.id
                  AND e2.embedding IS NOT NULL
                ORDER BY
                    -- Prefer the member with the most precise location so the
                    -- cluster dot doesn't sit on a country centroid when a
                    -- sibling row knows the actual city.
                    CASE e2.geo_precision
                        WHEN 'point'   THEN 0
                        WHEN 'city'    THEN 1
                        WHEN 'state'   THEN 2
                        WHEN 'country' THEN 3
                        ELSE 4
                    END ASC,
                    (e2.image_url IS NOT NULL) DESC,
                    e2.embedding <=> c.centroid_embedding ASC
                LIMIT 1
            ) e ON true
            WHERE c.last_seen >= %s
              AND c.event_count >= %s
              AND e.id IS NOT NULL
            ORDER BY coalesce(c.importance_score, 0) DESC,
                     c.event_count DESC,
                     c.last_seen DESC
            LIMIT %s
            """,
            (since, min_events, limit),
        )
        rows = cur.fetchall()

    out: list[ClusterOut] = []
    for r in rows:
        importance = r[14]
        event_count = r[8]
        breaking = event_count >= 10 or (importance is not None and importance >= 0.85)
        out.append(
            ClusterOut(
                id=r[0],
                title=r[1],
                summary=r[2],
                url=r[3],
                image_url=r[4],
                source_outlet=r[5],
                first_seen=r[6],
                last_seen=r[7],
                event_count=event_count,
                lat=r[9],
                lon=r[10],
                country_code=r[11],
                city=r[12],
                category=r[13],
                importance=importance,
                breaking=breaking,
                geo_precision=r[15],
            )
        )
    return out


@app.get("/clusters/{cluster_id}", response_model=ClusterDetailOut)
def cluster_detail(cluster_id: UUID) -> ClusterDetailOut:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary, first_seen, last_seen, event_count,
                   ST_Y(centroid_location::geometry) AS lat,
                   ST_X(centroid_location::geometry) AS lon,
                   primary_country, primary_category, importance_score
            FROM clusters
            WHERE id = %s
            """,
            (cluster_id,),
        )
        c = cur.fetchone()
        if c is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="cluster not found")

        cur.execute(
            """
            SELECT e.id, e.title, e.summary, e.url, e.image_url, e.source_outlet,
                   e.occurred_at, e.categories
            FROM events e
            JOIN clusters cl ON cl.id = e.cluster_id
            WHERE e.cluster_id = %s AND e.embedding IS NOT NULL
            ORDER BY e.embedding <=> cl.centroid_embedding
            LIMIT 25
            """,
            (cluster_id,),
        )
        members = cur.fetchall()

    importance = c[10]
    breaking = c[5] >= 10 or (importance is not None and importance >= 0.85)
    return ClusterDetailOut(
        id=c[0],
        title=c[1],
        summary=c[2],
        first_seen=c[3],
        last_seen=c[4],
        event_count=c[5],
        lat=c[6],
        lon=c[7],
        country_code=c[8],
        category=c[9],
        importance=importance,
        breaking=breaking,
        members=[
            ClusterMemberOut(
                id=m[0],
                title=m[1],
                summary=m[2],
                url=m[3],
                image_url=m[4],
                source_outlet=m[5],
                occurred_at=m[6],
                categories=list(m[7]) if m[7] else [],
            )
            for m in members
        ],
    )


@app.get("/markets", response_model=list[MarketOut])
def markets() -> list[MarketOut]:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, name, city, country_code,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   price, prev_close, change_pct, currency, updated_at
            FROM markets
            ORDER BY change_pct DESC NULLS LAST
            """
        )
        rows = cur.fetchall()
    return [
        MarketOut(
            symbol=r[0],
            name=r[1],
            city=r[2],
            country_code=r[3],
            lat=r[4],
            lon=r[5],
            price=float(r[6]) if r[6] is not None else None,
            prev_close=float(r[7]) if r[7] is not None else None,
            change_pct=r[8],
            currency=r[9],
            updated_at=r[10],
        )
        for r in rows
    ]


@app.get("/events/recent", response_model=list[EventOut])
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


@app.get("/events", response_model=list[EventOut])
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
