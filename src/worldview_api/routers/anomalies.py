"""Event-rate anomaly endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..db import get_pool
from ..schemas import AnomalyOut

router = APIRouter()


@router.get("/anomalies", response_model=list[AnomalyOut])
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
