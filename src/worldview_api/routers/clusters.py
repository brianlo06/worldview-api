"""Cluster list + detail endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response

from ..db import get_pool
from ..schemas import ClusterDetailOut, ClusterMemberOut, ClusterOut
from ..scoring import is_breaking, tier_where_clause

router = APIRouter()


@router.get("/clusters", response_model=list[ClusterOut])
def clusters(
    response: Response,
    hours: int = Query(48, ge=1, le=720),
    min_events: int = Query(1, ge=1, le=100),
    limit: int = Query(500, ge=1, le=5000),
    tier: Literal["all", "notable", "major", "top"] = Query("all"),
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
    tier_sql, tier_params = tier_where_clause(tier)
    tier_clause = f"AND {tier_sql}" if tier_sql else ""
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
              {tier_clause}
            ORDER BY coalesce(c.importance_score, 0) DESC,
                     c.event_count DESC,
                     c.last_seen DESC
            LIMIT %s
            """,
            (since, min_events, *tier_params, limit),
        )
        rows = cur.fetchall()

    out: list[ClusterOut] = []
    for r in rows:
        importance = r[14]
        event_count = r[8]
        breaking = is_breaking(event_count, importance)
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


@router.get("/clusters/{cluster_id}", response_model=ClusterDetailOut)
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
    breaking = is_breaking(c[5], importance)
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
