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
                       e.title
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
            ) = row

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
