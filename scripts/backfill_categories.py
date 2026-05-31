#!/usr/bin/env python
"""Backfill events.categories and clusters.primary_category after a regex fix.

Why this exists:

  The GKG categorization regex previously misclassified ~79% of events as
  `business` because the business pattern included `TAX_`, which is GDELT's
  taxonomy prefix (TAX_FNCACT, TAX_ETHNICITY, ...) rather than a content
  signal. After removing `TAX_` from the regex, existing rows still carry
  the old (wrong) categories — this script recomputes them from the stored
  GKG raw row.

  Secondary fix: clusters.primary_category is set from the first event at
  cluster creation and never updated. This script also resets it to the
  *mode* of member-event categories, so a cluster's color reflects its
  overall topic, not the arrival order of members.

Run modes:

  --dry-run   Compute everything, print the would-be summary, touch nothing.
  --apply     Actually update rows and commit.

Window:

  --since-hours <int>   How far back (hours) to recompute. Default 48.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter

from worldview_api.db import get_pool
from worldview_api.ingest.gdelt_gkg import _THEME_PATTERNS, themes_to_category

log = logging.getLogger("backfill_categories")

# Tiebreak priority for cluster primary_category — earlier wins ties.
# Mirrors the declaration order in _THEME_PATTERNS so the rule matches
# themes_to_category exactly.
_PRIORITY = {cat: i for i, (_, cat) in enumerate(_THEME_PATTERNS)}
_PRIORITY.setdefault("politics", len(_PRIORITY))


def _recompute_event_category(raw: dict | None) -> str | None:
    """Return the new primary category for a GKG event row, or None if raw is missing."""
    if not raw:
        return None
    themes = (raw.get("V2ENHANCEDTHEMES") or raw.get("V1THEMES") or "")
    return themes_to_category(themes)


def _mode_with_priority(categories: list[str]) -> str | None:
    """Most frequent category; ties broken by _THEME_PATTERNS priority order."""
    if not categories:
        return None
    counts = Counter(categories)
    # Sort key: (count desc, priority asc). Higher count wins; on tie, lower
    # priority index (declared earlier) wins.
    best = max(counts.items(), key=lambda kv: (kv[1], -_PRIORITY.get(kv[0], 99)))
    return best[0]


def backfill_events(
    since_hours: int, apply_changes: bool,
) -> tuple[int, int, Counter, Counter, dict[str, list[str]]]:
    """Recompute events.categories[0] for GKG events in the window.

    Returns (examined, updated, old_dist, new_dist, cluster_new_cats_by_id).
    cluster_new_cats_by_id holds the list of *new* categories per cluster,
    built in-memory so the dry-run cluster mode is honest.
    """
    pool = get_pool()
    examined = updated = 0
    old_dist: Counter = Counter()
    new_dist: Counter = Counter()
    cluster_cats: dict[str, list[str]] = {}

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, cluster_id, categories, raw
            FROM events
            WHERE source = 'gdelt_gkg'
              AND occurred_at > NOW() - (%s * INTERVAL '1 hour')
            """,
            (since_hours,),
        )
        rows = cur.fetchall()

    log.info("examining %d gdelt_gkg events in last %dh", len(rows), since_hours)

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        for ev_id, cluster_id, old_cats, raw in rows:
            examined += 1
            new_primary = _recompute_event_category(raw)
            if new_primary is None:
                continue
            old_primary = (old_cats[0] if old_cats else None)
            old_dist[old_primary or "(null)"] += 1
            new_dist[new_primary] += 1
            if cluster_id is not None:
                cluster_cats.setdefault(str(cluster_id), []).append(new_primary)
            if old_primary == new_primary:
                continue
            # Preserve any non-primary tags (e.g. "breaking") trailing the
            # primary in the categories array.
            tail = list(old_cats[1:]) if old_cats and len(old_cats) > 1 else []
            new_cats_array = [new_primary] + tail
            if apply_changes:
                cur.execute(
                    "UPDATE events SET categories = %s WHERE id = %s",
                    (new_cats_array, ev_id),
                )
            updated += 1
        if apply_changes:
            conn.commit()

    return examined, updated, old_dist, new_dist, cluster_cats


def reset_cluster_categories(
    cluster_cats: dict[str, list[str]], apply_changes: bool,
) -> tuple[int, Counter, Counter]:
    """Reset clusters.primary_category from the in-memory recomputed members.

    Returns (rows_updated, old_dist, new_dist).
    """
    if not cluster_cats:
        return 0, Counter(), Counter()

    pool = get_pool()
    updated = 0
    old_dist: Counter = Counter()
    new_dist: Counter = Counter()

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, primary_category FROM clusters WHERE id = ANY(%s::uuid[])",
            (list(cluster_cats.keys()),),
        )
        old_rows = {str(cid): old for cid, old in cur.fetchall()}

    log.info("recomputing primary_category for %d clusters", len(cluster_cats))

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        for cid, cats in cluster_cats.items():
            new_primary = _mode_with_priority(cats)
            if new_primary is None:
                continue
            old_primary = old_rows.get(cid)
            old_dist[old_primary or "(null)"] += 1
            new_dist[new_primary] += 1
            if old_primary == new_primary:
                continue
            if apply_changes:
                cur.execute(
                    "UPDATE clusters SET primary_category = %s WHERE id = %s",
                    (new_primary, cid),
                )
            updated += 1
        if apply_changes:
            conn.commit()

    return updated, old_dist, new_dist


def _format_dist(dist: Counter) -> str:
    total = sum(dist.values())
    if total == 0:
        return "  (no rows)"
    lines = []
    for cat in ["politics", "business", "conflict", "weather", "social", "quake", "(null)"]:
        n = dist.get(cat, 0)
        if n == 0 and cat == "(null)":
            continue
        pct = (n * 100 / total) if total else 0.0
        bar = "█" * int(round(pct / 2))
        lines.append(f"  {cat:>10}  {n:6d}  {pct:5.1f}%  {bar}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true",
        help="Compute the changes and print the summary; touch nothing.",
    )
    group.add_argument(
        "--apply", action="store_true",
        help="Actually update the rows and commit.",
    )
    parser.add_argument(
        "--since-hours", type=int, default=48,
        help="How far back (hours) to recompute. Default 48.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    examined, updated, ev_old, ev_new, cluster_cats = backfill_events(
        args.since_hours, args.apply
    )
    cluster_updated, cl_old, cl_new = reset_cluster_categories(
        cluster_cats, args.apply
    )

    print()
    print(f"mode:            {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"window:          last {args.since_hours} hours")
    print(f"events examined: {examined}")
    print(f"events updated:  {updated}")
    print(f"clusters touched: {len(cluster_cats)}")
    print(f"clusters updated: {cluster_updated}")
    print()
    print("event category distribution — BEFORE:")
    print(_format_dist(ev_old))
    print()
    print("event category distribution — AFTER:")
    print(_format_dist(ev_new))
    print()
    print("cluster primary_category distribution — BEFORE:")
    print(_format_dist(cl_old))
    print()
    print("cluster primary_category distribution — AFTER:")
    print(_format_dist(cl_new))
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
