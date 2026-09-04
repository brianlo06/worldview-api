"""NOAA NWS active weather alerts ingestion.

Pulls active alerts from api.weather.gov, maps severity to importance,
computes each alert's polygon centroid via PostGIS, and inserts as
weather-category events alongside the news firehose.

Free, no API key. NWS asks for a User-Agent including contact info.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Jsonb

from ..db import get_pool

log = logging.getLogger(__name__)

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_HEADERS = {
    "User-Agent": "worldview-dev/0.1 (https://github.com/local)",
    "Accept": "application/geo+json",
}

# Severity -> importance. NWS is a US-only firehose: ~225 alerts are active at
# any moment and ~1700 land in a 48h window, against ~1000 news articles per
# 15-minute GDELT slot. On the old scale a *Minor* alert scored 0.40 — above
# the median GKG article (0.55 max ~0.90 only for outliers) and well above the
# frontend's 0.3 floor — so routine US weather crowded world news off the globe
# and dominated clustering. Only genuinely severe weather should compete.
SEVERITY_IMPORTANCE: dict[str, float] = {
    "Extreme": 0.85,
    "Severe": 0.60,
    "Moderate": 0.35,
}

# Dropped at ingest rather than scored low: these are the bulk of the feed
# (about 60% of active alerts) and none of it is world news.
SKIPPED_SEVERITIES: frozenset[str] = frozenset({"Minor", "Unknown"})

# Event types that should pulse as "breaking" regardless of severity field
BREAKING_EVENTS: set[str] = {
    "Tornado Warning",
    "Hurricane Warning",
    "Hurricane Force Wind Warning",
    "Flash Flood Warning",
    "Storm Surge Warning",
    "Tsunami Warning",
    "Extreme Wind Warning",
    "Severe Thunderstorm Warning",
    "Blizzard Warning",
}


async def fetch_nws_alerts() -> list[dict]:
    async with httpx.AsyncClient(headers=NWS_HEADERS) as client:
        r = await client.get(NWS_ALERTS_URL, timeout=30)
        r.raise_for_status()
        return r.json().get("features", [])


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def ingest_nws_once() -> dict[str, int | str]:
    features = asyncio.run(fetch_nws_alerts())
    log.info("nws fetched %d alerts", len(features))

    pool = get_pool()
    inserted_raw = 0
    inserted_events = 0
    breaking_count = 0
    skipped = 0

    with pool.connection() as conn, conn.cursor() as cur:
        for feat in features:
            props = feat.get("properties") or {}
            geom = feat.get("geometry")
            alert_id = props.get("id") or feat.get("id")
            if not alert_id or not geom:
                skipped += 1
                continue

            event_type = props.get("event") or "Weather Alert"
            severity = props.get("severity") or "Unknown"
            area_desc = props.get("areaDesc") or ""
            headline = props.get("headline") or f"{event_type} · {area_desc[:80]}"
            description = props.get("description") or ""

            if severity in SKIPPED_SEVERITIES or severity not in SEVERITY_IMPORTANCE:
                skipped += 1
                continue

            occurred_at = _parse_iso(props.get("sent") or props.get("effective"))
            importance = SEVERITY_IMPORTANCE[severity]
            is_breaking = event_type in BREAKING_EVENTS or severity == "Extreme"
            cats = ["weather"] + (["breaking"] if is_breaking else [])
            if is_breaking:
                breaking_count += 1

            url_hash_str = hashlib.sha256(f"nws:{alert_id}".encode()).hexdigest()
            geom_json = json.dumps(geom)

            try:
                cur.execute(
                    """
                    INSERT INTO raw_events (source, source_id, payload)
                    VALUES ('nws', %s, %s)
                    ON CONFLICT (source, source_id) DO NOTHING
                    RETURNING id
                    """,
                    (alert_id, Jsonb(feat)),
                )
                raw_row = cur.fetchone()
                if raw_row is None:
                    # We've seen this alert before — refresh the event row
                    cur.execute(
                        """
                        UPDATE events
                        SET title = %s,
                            summary = %s,
                            importance = %s,
                            categories = %s,
                            scraped_at = NOW(),
                            scrape_status = 'ok'
                        WHERE url_hash = %s
                        """,
                        (headline[:240], description[:600], importance, cats, url_hash_str),
                    )
                    if cur.rowcount > 0:
                        inserted_events += 1  # treat refresh as a touch
                    continue
                inserted_raw += 1
                raw_id = raw_row[0]

                cur.execute(
                    """
                    INSERT INTO events (
                        raw_event_id, title, summary, url, url_hash,
                        source, source_outlet, occurred_at,
                        location, country_code, categories,
                        importance, geo_precision, raw,
                        scraped_at, scrape_status
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        'nws', 'NWS', %s,
                        ST_Centroid(ST_GeomFromGeoJSON(%s))::geography,
                        'US', %s,
                        %s, 'point', %s,
                        NOW(), 'ok'
                    )
                    ON CONFLICT (url_hash) DO UPDATE
                    SET title = EXCLUDED.title,
                        summary = EXCLUDED.summary,
                        importance = EXCLUDED.importance,
                        categories = EXCLUDED.categories
                    """,
                    (
                        raw_id, headline[:240], description[:600], None, url_hash_str,
                        occurred_at,
                        geom_json, cats,
                        importance, Jsonb(feat),
                    ),
                )
                if cur.rowcount > 0:
                    inserted_events += 1

                cur.execute(
                    "UPDATE raw_events SET processed_at = NOW() WHERE id = %s",
                    (raw_id,),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("nws insert error for %s: %s", alert_id, e)
                skipped += 1

        # Watermark for the source
        cur.execute(
            """
            INSERT INTO source_watermarks (source, last_seen_at, cursor)
            VALUES ('nws', %s, %s)
            ON CONFLICT (source) DO UPDATE
            SET last_seen_at = EXCLUDED.last_seen_at,
                cursor       = EXCLUDED.cursor,
                updated_at   = NOW()
            """,
            (datetime.now(timezone.utc), str(len(features))),
        )
        conn.commit()

    return {
        "status": "ok",
        "fetched": len(features),
        "inserted_raw": inserted_raw,
        "inserted_events": inserted_events,
        "breaking": breaking_count,
        "skipped": skipped,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(ingest_nws_once())


if __name__ == "__main__":
    main()
