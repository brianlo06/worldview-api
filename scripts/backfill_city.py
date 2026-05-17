"""Re-derive events.city and events.country_code for existing GKG rows.

The earlier SQL backfill naively took the *first* location in each
V2ENHANCEDLOCATIONS list, while the actual ingestion picks the
most-specific location (city > state > country). Mismatch produced
labels like "Texas · Venezuela". This rebuilds both fields from raw
data using the canonical pick_best_location logic.
"""
from __future__ import annotations

import logging

from worldview_api.db import get_pool
from worldview_api.ingest.gdelt_gkg import parse_locations, pick_best_location

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    pool = get_pool()
    fixed = 0

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, raw->>'V2ENHANCEDLOCATIONS'
            FROM events
            WHERE source = 'gdelt_gkg'
              AND raw->>'V2ENHANCEDLOCATIONS' IS NOT NULL
            """
        )
        rows = cur.fetchall()

    log.info("scanning %d GKG events", len(rows))

    with pool.connection() as conn, conn.cursor() as cur:
        for event_id, locs_str in rows:
            locs = parse_locations(locs_str)
            best = pick_best_location(locs)
            if best is None:
                continue
            _loc_type, loc_name, loc_cc, _, _ = best
            city = loc_name.split(",")[0].strip() if loc_name else None
            cc = loc_cc[:2] if loc_cc and len(loc_cc) >= 2 else None
            cur.execute(
                "UPDATE events SET city = %s, country_code = %s WHERE id = %s",
                (city, cc, event_id),
            )
            fixed += 1
        conn.commit()

    log.info("fixed %d events", fixed)


if __name__ == "__main__":
    main()
