"""Daily card-pool minting for the SCAN module.

Selects real clusters (via their representative event, same join the cluster
API uses), filters and dedupes them, assigns reality-derived tiers, and
snapshots every display field into game_card_pool — cards must stay
renderable after the 3-day retention job prunes their source rows.

Scheduling: run_all.py calls mint_if_needed() each ingest cycle (every 15
min); the first cycle after UTC midnight mints the day's pool and later
cycles no-op. `python -m worldview_api.game.mint [YYYY-MM-DD]` for manual runs.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from ..db import get_pool
from .card_images import cache_card_image
from . import rates as rates_cfg
from .logic import TIER_ORDER

log = logging.getLogger(__name__)

# ── pure helpers (unit-tested without a DB) ─────────────────────────────────

def is_grim(text: str | None, terms: list[str]) -> bool:
    """Case-insensitive substring match against the exclusion list."""
    if not text:
        return False
    low = text.lower()
    return any(t in low for t in terms)


def art_seed_for(cluster_id: str) -> int:
    """Deterministic 63-bit seed from the source cluster id."""
    digest = hashlib.sha256(str(cluster_id).encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def rare_countries(freq: dict[str, int]) -> set[str]:
    """Countries in the bottom quartile of the running frequency table.
    With no history yet, nothing is 'rare' — bumps start once data accrues."""
    if len(freq) < 8:
        return set()
    counts = sorted(freq.values())
    threshold = counts[max(0, len(counts) // 4 - 1)]
    return {c for c, n in freq.items() if n <= threshold}


def assign_tiers(cards: list[dict], freq: dict[str, int], mint_cfg: dict) -> None:
    """Mutates each card dict, setting 'tier'.

    Base band from importance percentile within the pool; bounded upward
    bumps (max +2 bands) for category scarcity (bottom-2 categories),
    country rarity, and point precision (half-bump). Legendary is only
    reachable via bumps from the epic band, then the count is normalized to
    [1, legendary_max] by promoting/demoting at the importance margin.
    """
    if not cards:
        return
    ordered = sorted(cards, key=lambda c: c.get("importance") or 0.0, reverse=True)
    n = len(ordered)
    cat_counts = Counter(c.get("category") or "?" for c in cards)
    # Scarce = bottom-2 categories that are also genuinely underrepresented
    # (≤ half the mean count) — a flat distribution has no scarce category.
    scarce_cats: set[str] = set()
    if len(cat_counts) > 2:
        mean = n / len(cat_counts)
        scarce_cats = {
            cat for cat, cnt in cat_counts.most_common()[-2:] if cnt <= mean / 2
        }
    rare = rare_countries(freq)

    for i, card in enumerate(ordered):
        p = i / n  # 0.0 = most important
        if p < 0.03:
            band = 3  # epic
        elif p < 0.12:
            band = 2  # rare
        elif p < 0.40:
            band = 1  # uncommon
        else:
            band = 0  # common
        bumps = 0.0
        if (card.get("category") or "?") in scarce_cats:
            bumps += 1.0
        if card.get("country") in rare:
            bumps += 1.0
        if card.get("geo_precision") == "point":
            bumps += 0.5
        band = min(4, band + min(2, int(bumps)))
        card["tier"] = TIER_ORDER[band]

    # Normalize legendary count to [1, legendary_max].
    legendary_max = int(mint_cfg.get("legendary_max", 3))
    legendaries = [c for c in ordered if c["tier"] == "legendary"]
    if not legendaries:
        for c in ordered:  # promote the most important epic
            if c["tier"] == "epic":
                c["tier"] = "legendary"
                break
        else:
            ordered[0]["tier"] = "legendary"
    elif len(legendaries) > legendary_max:
        for c in sorted(legendaries, key=lambda c: c.get("importance") or 0.0)[
            : len(legendaries) - legendary_max
        ]:
            c["tier"] = "epic"


def dedupe_candidates(cards: list[dict], cap: int) -> list[dict]:
    """Best-importance card per (country, category) pair, then top `cap`."""
    best: dict[tuple, dict] = {}
    for c in sorted(cards, key=lambda c: c.get("importance") or 0.0, reverse=True):
        key = (c.get("country"), c.get("category"))
        if key not in best:
            best[key] = c
    kept = sorted(best.values(), key=lambda c: c.get("importance") or 0.0, reverse=True)
    return kept[:cap]


# ── DB job ───────────────────────────────────────────────────────────────────

_CANDIDATE_SQL = """
    SELECT c.id,
           e.title,
           coalesce(e.summary, c.summary),
           ST_Y(e.location::geometry) AS lat,
           ST_X(e.location::geometry) AS lon,
           e.country_code,
           c.primary_category,
           c.importance_score,
           e.geo_precision,
           e.image_url,
           e.source_outlet
    FROM clusters c
    JOIN events e ON e.id = c.representative_event_id
    WHERE c.last_seen >= %s
      AND e.geo_precision IN ('point', 'city')
      AND e.location IS NOT NULL
      AND coalesce(c.importance_score, 0) >= %s
      AND e.title IS NOT NULL
"""

_CARD_KEYS = ("source_cluster_id", "headline", "summary", "lat", "lon",
              "country", "category", "importance", "geo_precision",
              "image_url", "source_outlet")


def _fetch_candidates(conn, since: datetime, floor: float) -> list[dict]:
    rows = conn.execute(_CANDIDATE_SQL, (since, floor)).fetchall()
    return [dict(zip(_CARD_KEYS, r)) for r in rows]


def _refresh_country_freq(conn) -> dict[str, int]:
    """Approximate a rolling 30-day per-country event count from daily
    increments: decay the running total by 1/30, then add the trailing 24h.
    (A true 30-day window is impossible — events are pruned after 3 days.)"""
    conn.execute(
        "UPDATE game_country_freq SET events_30d = GREATEST(0, "
        "(events_30d * 29) / 30), updated_at = NOW()"
    )
    conn.execute(
        """
        INSERT INTO game_country_freq (country, events_30d)
        SELECT country_code, count(*) FROM events
        WHERE occurred_at >= NOW() - interval '24 hours'
          AND country_code IS NOT NULL
        GROUP BY country_code
        ON CONFLICT (country) DO UPDATE
            SET events_30d = game_country_freq.events_30d + EXCLUDED.events_30d,
                updated_at = NOW()
        """
    )
    rows = conn.execute("SELECT country, events_30d FROM game_country_freq").fetchall()
    return dict(rows)


def mint_pool(pool_date: date | None = None) -> dict:
    """Mint the pool for `pool_date` (default: today UTC). Idempotent —
    ON CONFLICT DO NOTHING on (pool_date, source_cluster_id)."""
    pool_date = pool_date or datetime.now(timezone.utc).date()
    db = get_pool()
    with db.connection() as conn:
        cfg = rates_cfg.load_config(conn)["mint"]
        terms = [r[0].lower() for r in conn.execute(
            "SELECT term FROM game_grim_terms").fetchall()]
        freq = _refresh_country_freq(conn)

        now = datetime.now(timezone.utc)
        floor = float(cfg.get("importance_floor", 0.45))
        candidates = _fetch_candidates(conn, now - timedelta(hours=24), floor)
        window_h = 24
        if len(candidates) < int(cfg.get("sparse_min", 40)):
            candidates = _fetch_candidates(conn, now - timedelta(hours=72), floor)
            window_h = 72

        kept, excluded = [], 0
        for c in candidates:
            if is_grim(c["headline"], terms) or is_grim(c["summary"], terms):
                excluded += 1
            else:
                kept.append(c)

        pool = dedupe_candidates(kept, int(cfg.get("pool_cap", 120)))
        assign_tiers(pool, freq, cfg)

        inserted = 0
        for c in pool:
            cur = conn.execute(
                """
                INSERT INTO game_card_pool
                    (pool_date, source_cluster_id, tier, headline, summary,
                     lat, lon, country, category, importance, image_url,
                     source_outlet, art_seed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (pool_date, source_cluster_id) DO NOTHING
                RETURNING id
                """,
                (pool_date, c["source_cluster_id"], c["tier"], c["headline"],
                 c["summary"], c["lat"], c["lon"], c["country"], c["category"],
                 c["importance"], c.get("image_url"), c.get("source_outlet"),
                 art_seed_for(c["source_cluster_id"])),
            )
            row = cur.fetchone()
            if row:
                inserted += 1
                if cache_card_image(row[0], c.get("image_url")):
                    conn.execute(
                        "UPDATE game_card_pool SET has_image = TRUE WHERE id = %s",
                        (row[0],),
                    )
        conn.commit()

    stats = {
        "pool_date": str(pool_date),
        "window_h": window_h,
        "candidates": len(candidates),
        "excluded_grim": excluded,
        "pool_size": len(pool),
        "inserted": inserted,
        "tiers": dict(Counter(c["tier"] for c in pool)),
    }
    log.info("mint: %s", stats)
    return stats


def cache_images_for_pool(pool_date: date, limit: int = 120) -> dict:
    """Backfill/caches images for an already-minted pool while source events
    still exist. This lets a deploy add photos to today's pool without
    reminting or changing any rolls."""
    db = get_pool()
    checked = cached = 0
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id,
                   COALESCE(p.image_url, e.image_url) AS image_url,
                   COALESCE(p.source_outlet, e.source_outlet) AS source_outlet
            FROM game_card_pool p
            LEFT JOIN clusters c ON c.id = p.source_cluster_id
            LEFT JOIN events e ON e.id = c.representative_event_id
            WHERE p.pool_date = %s
              AND p.has_image = FALSE
              AND COALESCE(p.image_url, e.image_url) IS NOT NULL
            ORDER BY p.created_at DESC
            LIMIT %s
            """,
            (pool_date, limit),
        ).fetchall()
        for card_id, image_url, source_outlet in rows:
            checked += 1
            if cache_card_image(card_id, image_url):
                cached += 1
                conn.execute(
                    "UPDATE game_card_pool "
                    "SET image_url = %s, source_outlet = %s, has_image = TRUE "
                    "WHERE id = %s",
                    (image_url, source_outlet, card_id),
                )
        conn.commit()
    stats = {"pool_date": str(pool_date), "checked": checked, "cached": cached}
    if checked:
        log.info("mint image backfill: %s", stats)
    return stats


def mint_if_needed() -> dict | None:
    """Ingest-cycle hook: mint today's pool once per UTC day."""
    today = datetime.now(timezone.utc).date()
    db = get_pool()
    with db.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM game_card_pool WHERE pool_date = %s LIMIT 1", (today,)
        ).fetchone()
    if row:
        cache_images_for_pool(today)
        return None
    return mint_pool(today)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    print(mint_pool(target))
