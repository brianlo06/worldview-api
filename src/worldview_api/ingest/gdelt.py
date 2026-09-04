"""GDELT 2.0 events ingestion worker.

Pulls the latest 15-minute events CSV from GDELT's lastupdate.txt,
parses it, dedupes via url_hash + GDELT's GLOBALEVENTID, and inserts
into raw_events + events tables.

Idempotent: safe to run repeatedly; rows seen before are skipped.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

from ..db import get_pool
from .categories import breaking_from_row, cameo_to_category, importance_from_row
from .common import GDELT_LASTUPDATE_URL, gdelt_get, url_hash
from .common import parse_gdelt_timestamp as parse_gdelt_date

log = logging.getLogger(__name__)

# Canonical GDELT 2.0 events column ordering (61 columns).
# Source: http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf
EVENT_COLUMNS: tuple[str, ...] = (
    "GLOBALEVENTID", "SQLDATE", "MonthYear", "Year", "FractionDate",
    "Actor1Code", "Actor1Name", "Actor1CountryCode", "Actor1KnownGroupCode",
    "Actor1EthnicCode", "Actor1Religion1Code", "Actor1Religion2Code",
    "Actor1Type1Code", "Actor1Type2Code", "Actor1Type3Code",
    "Actor2Code", "Actor2Name", "Actor2CountryCode", "Actor2KnownGroupCode",
    "Actor2EthnicCode", "Actor2Religion1Code", "Actor2Religion2Code",
    "Actor2Type1Code", "Actor2Type2Code", "Actor2Type3Code",
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode",
    "QuadClass", "GoldsteinScale", "NumMentions", "NumSources",
    "NumArticles", "AvgTone",
    "Actor1Geo_Type", "Actor1Geo_FullName", "Actor1Geo_CountryCode",
    "Actor1Geo_ADM1Code", "Actor1Geo_ADM2Code", "Actor1Geo_Lat",
    "Actor1Geo_Long", "Actor1Geo_FeatureID",
    "Actor2Geo_Type", "Actor2Geo_FullName", "Actor2Geo_CountryCode",
    "Actor2Geo_ADM1Code", "Actor2Geo_ADM2Code", "Actor2Geo_Lat",
    "Actor2Geo_Long", "Actor2Geo_FeatureID",
    "ActionGeo_Type", "ActionGeo_FullName", "ActionGeo_CountryCode",
    "ActionGeo_ADM1Code", "ActionGeo_ADM2Code", "ActionGeo_Lat",
    "ActionGeo_Long", "ActionGeo_FeatureID",
    "DATEADDED", "SOURCEURL",
)


def fetch_latest_url() -> str:
    """Return the URL of the latest GDELT events CSV zip."""
    resp = gdelt_get(GDELT_LASTUPDATE_URL, timeout=20)
    first_line = resp.text.strip().splitlines()[0]
    parts = first_line.split()
    if len(parts) < 3:
        raise RuntimeError(f"Unexpected lastupdate.txt content: {first_line!r}")
    return parts[2]


def download_events_csv(url: str) -> list[dict[str, str]]:
    """Download and parse a GDELT events CSV zip into a list of row dicts."""
    resp = gdelt_get(url, timeout=60)
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            reader = csv.reader(text, delimiter="\t")
            for raw_row in reader:
                if len(raw_row) < len(EVENT_COLUMNS):
                    raw_row = raw_row + [""] * (len(EVENT_COLUMNS) - len(raw_row))
                rows.append(dict(zip(EVENT_COLUMNS, raw_row)))
    return rows


# ActionGeo_Type codes use the same numbering as GKG location types:
#   1=Country, 2=US State, 3=US City, 4=World City, 5=World State.
# 0 / empty means "no geocoded location" — GDELT would also leave the
# lat/lon empty in that case, which we've already filtered for upstream.
_ACTION_GEO_TYPE_TO_PRECISION: dict[str, str] = {
    "1": "country",
    "2": "state",
    "3": "city",
    "4": "city",
    "5": "state",
}


def _action_geo_precision(action_geo_type: str | None) -> str:
    """Map GDELT events ActionGeo_Type to a geo_precision label."""
    return _ACTION_GEO_TYPE_TO_PRECISION.get(
        (action_geo_type or "").strip(), "country"
    )


def _has_alpha(s: str) -> bool:
    """True iff s contains at least one Unicode letter.

    Unicode-aware (not ASCII-only) so non-Latin headlines — Turkish, Arabic,
    Chinese, etc. — count as real titles. A purely numeric article ID (the
    junk we're guarding against) has no letters in any script.
    """
    return any(ch.isalpha() for ch in s)


def humanize_url(url: str) -> tuple[str, str]:
    """Best-effort title and source-outlet from a URL.

    GDELT doesn't carry headlines; this gives readable placeholders until we
    wire up a scraping/GKG pipeline.
    """
    parsed = urlparse(url)
    outlet = (parsed.netloc or "").lower()
    if outlet.startswith("www."):
        outlet = outlet[4:]
    parts = [p for p in parsed.path.split("/") if p]
    last = parts[-1] if parts else outlet
    last = last.split("?")[0].split("#")[0]
    # Strip a single trailing extension like .html, .htm, .php
    if "." in last:
        head, _, tail = last.rpartition(".")
        if 0 < len(tail) <= 5 and head:
            last = head
    title = last.replace("-", " ").replace("_", " ").strip()
    if len(title) > 140:
        title = title[:140]
    # A slug with no letters (e.g. a numeric article ID like "3952366") is not
    # a usable placeholder — fall back to the outlet so we never display a bare
    # number. Enrichment will replace it with the real og:title if it can.
    if not title or not _has_alpha(title):
        title = outlet or "untitled"
    return title.title(), outlet


def ingest_once() -> dict[str, Any]:
    """Pull the latest GDELT events file once and load it into Postgres."""
    url = fetch_latest_url()
    log.info("gdelt latest: %s", url)

    pool = get_pool()

    # Quick watermark check — skip if we already processed this URL
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM source_watermarks WHERE source = 'gdelt'"
        )
        row = cur.fetchone()
    if row and row[0] == url:
        return {"status": "already_processed", "url": url, "rows": 0}

    rows = download_events_csv(url)
    log.info("gdelt parsed %d rows", len(rows))

    inserted_raw = 0
    inserted_events = 0
    skipped = 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                lat_s = (r.get("ActionGeo_Lat") or "").strip()
                lon_s = (r.get("ActionGeo_Long") or "").strip()
                src_url = (r.get("SOURCEURL") or "").strip()
                if not lat_s or not lon_s or not src_url:
                    skipped += 1
                    continue
                try:
                    lat = float(lat_s)
                    lon = float(lon_s)
                except ValueError:
                    skipped += 1
                    continue
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    skipped += 1
                    continue

                global_id = (r.get("GLOBALEVENTID") or "").strip()
                if not global_id:
                    skipped += 1
                    continue
                u_hash = url_hash(src_url)
                title, outlet = humanize_url(src_url)
                occurred_at = parse_gdelt_date(
                    r.get("DATEADDED", "") or r.get("SQLDATE", "")
                )
                country = (r.get("ActionGeo_CountryCode") or "").strip() or None
                if country and len(country) > 2:
                    country = country[:2]
                # Map the ActionGeo_Type code to a geo_precision label
                # so the frontend can dim country-centroid dots.
                geo_precision = _action_geo_precision(r.get("ActionGeo_Type"))
                category = cameo_to_category(r.get("EventRootCode", "").strip())
                importance = importance_from_row(r)
                is_breaking = breaking_from_row(r)
                cats = [category] + (["breaking"] if is_breaking else [])

                try:
                    cur.execute(
                        """
                        INSERT INTO raw_events (source, source_id, payload)
                        VALUES ('gdelt', %s, %s)
                        ON CONFLICT (source, source_id) DO NOTHING
                        RETURNING id
                        """,
                        (global_id, Jsonb(r)),
                    )
                    raw_id_row = cur.fetchone()
                    if raw_id_row is None:
                        skipped += 1
                        continue
                    inserted_raw += 1
                    raw_id = raw_id_row[0]

                    cur.execute(
                        """
                        INSERT INTO events (
                            raw_event_id, title, url, url_hash,
                            source, source_outlet, occurred_at,
                            location, country_code, categories,
                            importance, geo_precision, raw
                        )
                        VALUES (
                            %s, %s, %s, %s,
                            'gdelt', %s, %s,
                            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                            %s, %s,
                            %s, %s, %s
                        )
                        ON CONFLICT (url_hash) DO NOTHING
                        """,
                        (
                            raw_id, title, src_url, u_hash,
                            outlet, occurred_at,
                            lon, lat,
                            country, cats,
                            importance, geo_precision, Jsonb(r),
                        ),
                    )
                    if cur.rowcount > 0:
                        inserted_events += 1

                    cur.execute(
                        "UPDATE raw_events SET processed_at = NOW() WHERE id = %s",
                        (raw_id,),
                    )
                except Exception as e:
                    log.warning("row error for %s: %s", global_id, e)
                    skipped += 1

            cur.execute(
                """
                INSERT INTO source_watermarks (source, last_seen_at, cursor)
                VALUES ('gdelt', %s, %s)
                ON CONFLICT (source) DO UPDATE
                SET last_seen_at = EXCLUDED.last_seen_at,
                    cursor       = EXCLUDED.cursor,
                    updated_at   = NOW()
                """,
                (datetime.now(timezone.utc), url),
            )
        conn.commit()

    return {
        "status": "ok",
        "url": url,
        "parsed": len(rows),
        "inserted_raw": inserted_raw,
        "inserted_events": inserted_events,
        "skipped": skipped,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    result = ingest_once()
    print(result)


if __name__ == "__main__":
    main()
