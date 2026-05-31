#!/usr/bin/env python
"""Backfill events.importance and clusters.importance_score after a rescale.

Why a backfill exists:

  Live ingestion ratchets clusters.importance_score with
  GREATEST(coalesce(importance_score, 0), new). After a rescale of
  importance_from_row, every cluster keeps its old (saturated) score
  forever — the live path can only push the value *up*. To pick up the
  new distribution we need to (a) recompute events.importance from the
  stored raw GKG row and (b) reset clusters.importance_score from the
  new event-level values. This script does both, in one window-bounded
  pass.

  After this script runs once, the GREATEST(...) ratchet resumes from
  the backfilled values — the invariant is broken exactly once.

Run modes:

  --dry-run   Compute everything, print the would-be summary, touch nothing.
  --apply     Actually update rows and commit.

Window:

  --since-hours <int>   How far back (in hours) to recompute. Default 48 — the
                        retention window the dashboard actually queries.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter

from worldview_api.db import get_pool
from worldview_api.ingest.gdelt_gkg import importance_from_row, parse_locations

log = logging.getLogger("backfill_importance")


BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<0.30",     0.0,  0.30),
    ("0.30-0.45", 0.30, 0.45),
    ("0.45-0.60", 0.45, 0.60),
    ("0.60-0.75", 0.60, 0.75),
    ("0.75-0.90", 0.75, 0.90),
    (">=0.90",    0.90, 1.0001),  # closed on the right edge
)


def _bucket(score: float) -> str:
    for label, lo, hi in BUCKETS:
        if lo <= score < hi:
            return label
    return "<0.30"


def _recompute_event_importance(raw: dict | None) -> float | None:
    """Return a new importance value for a GKG event row, or None if raw is missing."""
    if not raw:
        return None
    themes = raw.get("V2ENHANCEDTHEMES") or raw.get("V1THEMES") or ""
    theme_count = sum(1 for t in themes.split(";") if t.strip())
    loc_count = len(parse_locations(raw.get("V2ENHANCEDLOCATIONS")))
    return importance_from_row(raw, theme_count, loc_count)


def backfill_events(
    since_hours: int, apply_changes: bool,
) -> tuple[int, int, Counter, dict[str, float]]:
    """Recompute events.importance for GKG events in the window.

    Returns (examined, updated, event_distribution, cluster_new_max_by_id).
    cluster_new_max_by_id holds the *recomputed* MAX(importance) per cluster,
    built in-memory so the dry-run is honest (the DB still holds old values
    until --apply).
    """
    pool = get_pool()
    examined = updated = 0
    dist: Counter = Counter()
    cluster_max: dict[str, float] = {}

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, cluster_id, importance, raw
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
        for ev_id, cluster_id, old_imp, raw in rows:
            examined += 1
            new_imp = _recompute_event_importance(raw)
            if new_imp is None:
                continue
            dist[_bucket(new_imp)] += 1
            if cluster_id is not None:
                cid_str = str(cluster_id)
                prev = cluster_max.get(cid_str, 0.0)
                if new_imp > prev:
                    cluster_max[cid_str] = new_imp
            if old_imp is not None and abs(old_imp - new_imp) < 1e-6:
                continue
            if apply_changes:
                cur.execute(
                    "UPDATE events SET importance = %s WHERE id = %s",
                    (new_imp, ev_id),
                )
            updated += 1
        if apply_changes:
            conn.commit()

    return examined, updated, dist, cluster_max


def reset_cluster_scores(
    cluster_max: dict[str, float], apply_changes: bool,
) -> tuple[int, Counter]:
    """Reset clusters.importance_score using the in-memory recomputed MAXes.

    Returns (rows_updated, distribution_counter_for_new_cluster_scores).
    """
    if not cluster_max:
        return 0, Counter()

    pool = get_pool()
    updated = 0
    dist: Counter = Counter()

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, importance_score FROM clusters WHERE id = ANY(%s::uuid[])",
            (list(cluster_max.keys()),),
        )
        old_rows = {str(cid): old for cid, old in cur.fetchall()}

    log.info("recomputing importance_score for %d clusters", len(cluster_max))

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        for cid, new_score in cluster_max.items():
            dist[_bucket(new_score)] += 1
            old_score = old_rows.get(cid)
            if old_score is not None and abs(float(old_score) - new_score) < 1e-6:
                continue
            if apply_changes:
                cur.execute(
                    "UPDATE clusters SET importance_score = %s WHERE id = %s",
                    (new_score, cid),
                )
            updated += 1
        if apply_changes:
            conn.commit()

    return updated, dist


def _format_dist(dist: Counter) -> str:
    total = sum(dist.values())
    if total == 0:
        return "  (no rows)"
    lines = []
    for label, _, _ in BUCKETS:
        n = dist[label]
        pct = (n * 100 / total) if total else 0.0
        bar = "█" * int(round(pct / 2))  # 50% → 25 chars wide
        lines.append(f"  {label:>10}  {n:6d}  {pct:5.1f}%  {bar}")
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

    examined, updated, ev_dist, cluster_max = backfill_events(
        args.since_hours, args.apply
    )
    cluster_updated, cl_dist = reset_cluster_scores(cluster_max, args.apply)

    print()
    print(f"mode:           {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"window:         last {args.since_hours} hours")
    print(f"events examined: {examined}")
    print(f"events updated:  {updated}")
    print(f"clusters touched: {len(cluster_max)}")
    print(f"clusters updated: {cluster_updated}")
    print()
    print("event importance distribution (new values):")
    print(_format_dist(ev_dist))
    print()
    print("cluster importance_score distribution (new values):")
    print(_format_dist(cl_dist))
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
