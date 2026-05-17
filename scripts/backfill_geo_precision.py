#!/usr/bin/env python
"""Backfill events.geo_precision and re-score GKG locations.

Two passes:

  GDELT events (source='gdelt'):
    Pull ActionGeo_Type out of raw_events.payload and map it to a
    geo_precision label. Don't touch the lat/lon — there's only one
    coordinate per row, so there's nothing to re-score.

  GDELT GKG (source='gdelt_gkg'):
    Re-run pick_best_location() against the original V2ENHANCEDLOCATIONS
    using the new scorer. Update location / city / country_code /
    geo_precision when the winning location changes.

  NWS (source='nws'):
    Set geo_precision='point' on existing rows.

Run modes:

  --dry-run    Compute the changes and write a diff CSV; touch nothing.
  --apply      Actually update the rows.

Always writes a CSV to /tmp/worldview-geo-backfill-<timestamp>.csv with one
row per *change*: id, source, title, old_lat/lon/city/precision, new_lat/lon/city/precision.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from worldview_api.config import settings
from worldview_api.db import get_pool
from worldview_api.ingest.gdelt import _action_geo_precision
from worldview_api.ingest.gdelt_gkg import (
    parse_locations,
    pick_best_location,
    type_to_precision,
)

log = logging.getLogger("backfill_geo_precision")


def _csv_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"/tmp/worldview-geo-backfill-{ts}.csv")


def backfill_gdelt(apply_changes: bool, diff_writer: csv.writer) -> dict[str, int]:
    """Set geo_precision on existing source='gdelt' rows from ActionGeo_Type."""
    pool = get_pool()
    changed = unchanged = examined = 0
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.title, e.geo_precision, re.payload->>'ActionGeo_Type'
            FROM events e
            JOIN raw_events re ON re.id = e.raw_event_id
            WHERE e.source = 'gdelt'
            """
        )
        rows = cur.fetchall()

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        for ev_id, title, old_precision, action_type in rows:
            examined += 1
            new_precision = _action_geo_precision(action_type)
            if old_precision == new_precision:
                unchanged += 1
                continue
            diff_writer.writerow(
                [
                    str(ev_id),
                    "gdelt",
                    (title or "")[:80],
                    "",  # old lat
                    "",  # old lon
                    "",  # old city
                    old_precision or "",
                    "",  # new lat
                    "",  # new lon
                    "",  # new city
                    new_precision,
                ]
            )
            if apply_changes:
                cur.execute(
                    "UPDATE events SET geo_precision = %s WHERE id = %s",
                    (new_precision, ev_id),
                )
            changed += 1
        if apply_changes:
            conn.commit()

    return {"examined": examined, "changed": changed, "unchanged": unchanged}


def backfill_gkg(apply_changes: bool, diff_writer: csv.writer) -> dict[str, int]:
    """Re-score GKG locations and update events.location / city / precision."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id,
                   e.title,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.city,
                   e.country_code,
                   e.geo_precision,
                   re.payload->>'V2ENHANCEDLOCATIONS'
            FROM events e
            JOIN raw_events re ON re.id = e.raw_event_id
            WHERE e.source = 'gdelt_gkg'
            """
        )
        rows = cur.fetchall()

    examined = changed = unchanged = no_better = 0
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        for (
            ev_id,
            title,
            old_lat,
            old_lon,
            old_city,
            old_country,
            old_precision,
            loc_str,
        ) in rows:
            examined += 1
            locs = parse_locations(loc_str)
            best = pick_best_location(locs)
            if best is None:
                no_better += 1
                continue
            loc_type, loc_name, loc_cc, lat, lon, _off = best
            new_precision = type_to_precision(loc_type)
            new_city = (
                loc_name.split(",")[0].strip()
                if loc_name and new_precision != "country"
                else None
            )
            new_country = loc_cc[:2] if loc_cc and len(loc_cc) >= 2 else None

            # Treat as changed only if something materially differs.
            changed_now = (
                round(old_lat or 0, 3) != round(lat, 3)
                or round(old_lon or 0, 3) != round(lon, 3)
                or (old_city or None) != (new_city or None)
                or (old_country or None) != (new_country or None)
                or (old_precision or None) != new_precision
            )
            if not changed_now:
                unchanged += 1
                continue

            diff_writer.writerow(
                [
                    str(ev_id),
                    "gdelt_gkg",
                    (title or "")[:80],
                    f"{old_lat:.3f}" if old_lat is not None else "",
                    f"{old_lon:.3f}" if old_lon is not None else "",
                    old_city or "",
                    old_precision or "",
                    f"{lat:.3f}",
                    f"{lon:.3f}",
                    new_city or "",
                    new_precision,
                ]
            )
            if apply_changes:
                cur.execute(
                    """
                    UPDATE events
                    SET location      = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        city          = %s,
                        country_code  = %s,
                        geo_precision = %s
                    WHERE id = %s
                    """,
                    (lon, lat, new_city, new_country, new_precision, ev_id),
                )
            changed += 1
        if apply_changes:
            conn.commit()

    return {
        "examined": examined,
        "changed": changed,
        "unchanged": unchanged,
        "no_locations": no_better,
    }


def backfill_nws(apply_changes: bool, diff_writer: csv.writer) -> dict[str, int]:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, geo_precision FROM events WHERE source = 'nws'"
        )
        rows = cur.fetchall()

    examined = changed = unchanged = 0
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        for ev_id, title, old_precision in rows:
            examined += 1
            if old_precision == "point":
                unchanged += 1
                continue
            diff_writer.writerow(
                [
                    str(ev_id),
                    "nws",
                    (title or "")[:80],
                    "", "", "", old_precision or "",
                    "", "", "", "point",
                ]
            )
            if apply_changes:
                cur.execute(
                    "UPDATE events SET geo_precision = 'point' WHERE id = %s",
                    (ev_id,),
                )
            changed += 1
        if apply_changes:
            conn.commit()

    return {"examined": examined, "changed": changed, "unchanged": unchanged}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the changes and write the diff CSV; touch nothing.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Actually update the rows.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    # Touch the DB URL config early to surface any env problems.
    _ = settings.database_url

    diff_file = _csv_path()
    with diff_file.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "id",
                "source",
                "title",
                "old_lat",
                "old_lon",
                "old_city",
                "old_precision",
                "new_lat",
                "new_lon",
                "new_city",
                "new_precision",
            ]
        )
        log.info("mode: %s", "APPLY" if args.apply else "DRY-RUN")

        log.info("[1/3] gdelt events …")
        s1 = backfill_gdelt(args.apply, writer)
        log.info("    %s", s1)

        log.info("[2/3] gdelt_gkg …")
        s2 = backfill_gkg(args.apply, writer)
        log.info("    %s", s2)

        log.info("[3/3] nws …")
        s3 = backfill_nws(args.apply, writer)
        log.info("    %s", s3)

    log.info("diff written to %s", diff_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
