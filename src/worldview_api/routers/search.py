"""Semantic cluster search."""

from __future__ import annotations

from fastapi import APIRouter

from ..db import get_pool
from ..schemas import SearchRequest, SearchResultOut
from ..scoring import is_breaking

router = APIRouter()


@router.post("/search", response_model=list[SearchResultOut])
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

    from ..embed.embed import embed_texts

    # pgvector's <=> operator wants a vector-typed operand; np.ndarray adapts
    # correctly via the pgvector psycopg adapter (list[float] would adapt as
    # a double precision[] and the operator wouldn't match).
    query_vec = np.asarray(embed_texts([q])[0], dtype=np.float32)
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
            WHERE c.last_seen > NOW() - (%s * INTERVAL '1 hour')
              AND e.id IS NOT NULL
            ORDER BY c.centroid_embedding <=> %s
            LIMIT %s
            """,
            (query_vec, body.hours, query_vec, body.limit),
        )
        rows = cur.fetchall()

    results: list[SearchResultOut] = []
    for r in rows:
        sim = r[13] if r[13] is not None else 0.0
        if sim < body.min_similarity:
            continue
        importance = r[12]
        event_count = r[10]
        breaking = is_breaking(event_count, importance)
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
