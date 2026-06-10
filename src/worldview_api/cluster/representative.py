"""Maintain clusters.representative_event_id (see sql/010).

The representative is the member event the frontend shows for the cluster:
most precise location first (a city dot beats a country centroid), then
has-image, then closest to the centroid embedding. It changes when members
join (centroid moves, last_seen bumps) or when enrichment fills in an
image/title — both of which only happen to recently-active clusters, so each
run refreshes a sliding window of recent clusters rather than the whole table.
"""

from __future__ import annotations

import logging

from ..db import get_pool

log = logging.getLogger(__name__)

# Clusters quiet for longer than this keep their last pick. Members only
# arrive while a cluster is active (joining bumps last_seen), and enrichment
# touches recent events, so an inactive cluster's representative is final.
DEFAULT_WINDOW_HOURS = 6


def refresh_representatives(window_hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, int | str]:
    """Recompute representative_event_id for recently-active clusters."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE clusters c
            SET representative_event_id = (
                SELECT e2.id
                FROM events e2
                WHERE e2.cluster_id = c.id
                  AND e2.embedding IS NOT NULL
                ORDER BY
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
            )
            WHERE c.last_seen > NOW() - (%s * INTERVAL '1 hour')
            """,
            (window_hours,),
        )
        refreshed = cur.rowcount
        conn.commit()
    return {"status": "ok", "refreshed": refreshed, "window_hours": window_hours}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(refresh_representatives())


if __name__ == "__main__":
    main()
