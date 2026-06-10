"""Event-rate anomaly endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..db import get_pool
from ..schemas import AnomalyDriverStory, AnomalyOut

router = APIRouter()


@router.get("/anomalies", response_model=list[AnomalyOut])
def anomalies() -> list[AnomalyOut]:
    """Active regions whose recent event rate has spiked past baseline+3σ.

    Each anomaly carries its driver clusters (id + title, the stories actually
    driving the spike) so the frontend can link straight to them, plus the
    one-line synopsis generated at detection time.
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
                   a.synopsis,
                   coalesce(
                     (
                       SELECT jsonb_agg(
                                jsonb_build_object('cluster_id', c.id, 'title', c.title)
                                ORDER BY c.event_count DESC
                              )
                       FROM clusters c
                       WHERE c.id = ANY(a.driver_cluster_ids)
                     ),
                     '[]'::jsonb
                   ) AS driver_stories
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
            synopsis=r[9],
            driver_stories=[AnomalyDriverStory(**d) for d in (r[10] or [])][:4],
        )
        for r in rows
    ]
