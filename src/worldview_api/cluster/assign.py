"""Greedy online cluster assignment via pgvector kNN.

For each unclustered event:
  1. Find nearest cluster centroid within the configured time window.
  2. If cosine similarity ≥ threshold, join + update centroid incrementally.
  3. Otherwise, spawn a new cluster with this event as the seed.

Incremental centroid update:
  new_centroid = (n * old_centroid + new_embedding) / (n + 1)

Idempotent: re-running only processes events still missing a cluster_id.
"""

from __future__ import annotations

import logging

import numpy as np

from ..config import settings
from ..db import get_pool

log = logging.getLogger(__name__)

# NWS alerts cluster by storm system, not text similarity. Alert text is
# templated ("SVRTOP The National Weather Service in ... has issued a ..."),
# so every same-type warning embeds nearly identically and the kNN path
# snowballs days of nationwide alerts into one mega-cluster (observed at
# 1,285 members). Instead, an alert joins the cluster of the nearest recent
# alert of the SAME alert type (raw->properties->event) within this radius.
NWS_JOIN_RADIUS_M = 300_000
# Member must be this recent — separates today's squall line from
# yesterday's in the same place.
NWS_JOIN_RECENT_HOURS = 6
# Never join a cluster older than this, so a multi-day weather pattern rolls
# over into fresh clusters daily instead of accumulating forever (an
# ever-growing event_count keeps a cluster permanently "breaking").
NWS_CLUSTER_MAX_AGE_HOURS = 24


def _storm_system_candidate(
    pool, alert_type: str, ev_time, ev_lat: float, ev_lon: float
) -> list[tuple]:
    """Find the storm-system cluster for an NWS alert.

    Returns rows shaped like the embedding candidate query —
    (cluster_id, centroid_embedding, event_count, similarity) — with
    similarity pinned to 1.0 so the caller's join branch applies unchanged.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id,
                   c.centroid_embedding,
                   c.event_count,
                   1.0::float8 AS cos_sim
            FROM events e
            JOIN clusters c ON c.id = e.cluster_id
            WHERE e.source = 'nws'
              AND e.raw->'properties'->>'event' = %s
              AND e.occurred_at > %s - INTERVAL '{NWS_JOIN_RECENT_HOURS} hours'
              AND c.first_seen > %s - INTERVAL '{NWS_CLUSTER_MAX_AGE_HOURS} hours'
              AND ST_DWithin(
                    e.location,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s
                  )
            ORDER BY ST_Distance(
                e.location,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            ) ASC
            LIMIT 1
            """,
            (
                alert_type, ev_time, ev_time,
                ev_lon, ev_lat, NWS_JOIN_RADIUS_M,
                ev_lon, ev_lat,
            ),
        )
        return cur.fetchall()


def cluster_assign_once(
    threshold: float | None = None,
    window_hours: int | None = None,
    batch_size: int = 100,
) -> dict[str, int | str]:
    threshold = threshold if threshold is not None else settings.cluster_threshold
    window_hours = (
        window_hours if window_hours is not None else settings.cluster_window_hours
    )
    pool = get_pool()
    joined = 0
    created = 0

    while True:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id,
                       e.embedding,
                       e.occurred_at,
                       ST_Y(e.location::geometry) AS lat,
                       ST_X(e.location::geometry) AS lon,
                       e.country_code,
                       e.categories,
                       e.importance,
                       e.title,
                       e.source,
                       e.raw->'properties'->>'event' AS nws_alert_type
                FROM events e
                WHERE e.embedding IS NOT NULL
                  AND e.cluster_id IS NULL
                ORDER BY e.occurred_at ASC
                LIMIT %s
                """,
                (batch_size,),
            )
            batch = cur.fetchall()

        if not batch:
            break

        for row in batch:
            (
                ev_id,
                ev_emb,
                ev_time,
                ev_lat,
                ev_lon,
                ev_country,
                ev_cats,
                ev_imp,
                ev_title,
                ev_source,
                ev_alert_type,
            ) = row

            if ev_source == "nws" and ev_alert_type:
                # Structured storm-system match — see _storm_system_candidate.
                candidates = _storm_system_candidate(
                    pool, ev_alert_type, ev_time, ev_lat, ev_lon
                )
            else:
                # Look up the nearest cluster centroid within the time window
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT id,
                               centroid_embedding,
                               event_count,
                               1 - (centroid_embedding <=> %s) AS cos_sim
                        FROM clusters
                        WHERE last_seen > %s - INTERVAL '{window_hours} hours'
                        ORDER BY centroid_embedding <=> %s
                        LIMIT 1
                        """,
                        (ev_emb, ev_time, ev_emb),
                    )
                    candidates = cur.fetchall()

            if candidates and candidates[0][3] is not None and candidates[0][3] >= threshold:
                cluster_id, c_emb, c_count, _sim = candidates[0]
                # Incremental centroid update — both arrays come back from pgvector as numpy
                new_centroid = (np.asarray(c_emb) * c_count + np.asarray(ev_emb)) / (
                    c_count + 1
                )

                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE clusters
                        SET centroid_embedding = %s,
                            event_count        = event_count + 1,
                            last_seen          = GREATEST(last_seen, %s),
                            importance_score   = GREATEST(
                              coalesce(importance_score, 0),
                              coalesce(%s::real, 0)
                            )
                        WHERE id = %s
                        """,
                        (new_centroid.astype(np.float32), ev_time, ev_imp, cluster_id),
                    )
                    cur.execute(
                        "UPDATE events SET cluster_id = %s WHERE id = %s",
                        (cluster_id, ev_id),
                    )
                    conn.commit()
                joined += 1
            else:
                # Strip "breaking" from the categories list so the cluster's
                # primary category reflects the actual topic, not the flag
                primary_cat = (
                    next((c for c in (ev_cats or []) if c != "breaking"), None)
                )
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO clusters (
                            title, summary, first_seen, last_seen, event_count,
                            centroid_embedding, centroid_location,
                            primary_country, primary_category, importance_score
                        )
                        VALUES (
                            %s, NULL, %s, %s, 1,
                            %s,
                            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                            %s, %s, %s
                        )
                        RETURNING id
                        """,
                        (
                            ev_title,
                            ev_time,
                            ev_time,
                            ev_emb,
                            ev_lon,
                            ev_lat,
                            ev_country,
                            primary_cat,
                            ev_imp,
                        ),
                    )
                    cluster_id = cur.fetchone()[0]
                    cur.execute(
                        "UPDATE events SET cluster_id = %s WHERE id = %s",
                        (cluster_id, ev_id),
                    )
                    conn.commit()
                created += 1

        log.info(
            "cluster batch: %d joined, %d new (running %d/%d)",
            joined,
            created,
            joined,
            created,
        )

    return {"status": "ok", "joined": joined, "created": created}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(cluster_assign_once())


if __name__ == "__main__":
    main()
