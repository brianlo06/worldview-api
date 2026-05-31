"""Cross-cutting scoring helpers used by the API endpoints."""

from __future__ import annotations

# Threshold sits above the typical band of the rescaled importance distribution
# (median ~0.55), so the importance branch fires only on top-of-distribution
# events. event_count is an independent escape hatch — a story covered by
# many outlets is breaking even when its importance score is moderate.
BREAKING_EVENT_COUNT_THRESHOLD = 10
BREAKING_IMPORTANCE_THRESHOLD = 0.92


def is_breaking(event_count: int, importance: float | None) -> bool:
    """Whether a cluster qualifies as breaking news.

    Two independent branches:
      - broad coverage (event_count >= 10)
      - top-of-distribution importance (>= 0.92)
    """
    if event_count >= BREAKING_EVENT_COUNT_THRESHOLD:
        return True
    if importance is not None and importance >= BREAKING_IMPORTANCE_THRESHOLD:
        return True
    return False


# keep in sync with passesTier in worldview/src/globe/tiers.ts
# Thresholds are calibrated against the post-recalibrate importance
# distribution (median ~0.55). NOTABLE sits at the data's natural cliff
# (imp >= 0.65) so it produces ~1800 matches, below the 2000 frontend cap.
TIER_NOTABLE_IMPORTANCE = 0.65
TIER_MAJOR_IMPORTANCE = 0.68
TIER_TOP_IMPORTANCE = 0.75


def tier_where_clause(tier: str) -> tuple[str, list]:
    """Build the SQL WHERE fragment + bound params for a significance tier.

    Predicates mirror the frontend `passesTier` in worldview/src/globe/tiers.ts.
    The returned fragment is intended to be ANDed onto an existing WHERE clause;
    when `tier == "all"`, the fragment is empty (no additional constraint).
    """
    if tier == "all":
        return "", []
    if tier == "notable":
        return "coalesce(c.importance_score, 0) >= %s", [TIER_NOTABLE_IMPORTANCE]
    if tier == "major":
        return "coalesce(c.importance_score, 0) >= %s", [TIER_MAJOR_IMPORTANCE]
    if tier == "top":
        return "coalesce(c.importance_score, 0) >= %s", [TIER_TOP_IMPORTANCE]
    raise ValueError(f"unknown tier: {tier!r}")
