"""Pure game logic — no DB, no I/O, no randomness sources.

Every function takes its inputs (including random fractions and clock values)
as arguments, so the whole rates/pity/streak surface is unit-testable and a
pull can be re-derived from its logged seed for audit.
"""

from __future__ import annotations

from datetime import date, timedelta

TIER_ORDER = ("common", "uncommon", "rare", "epic", "legendary")

# 16 hex chars = 64 bits of seed material per roll.
_SEED_HEX_CHARS = 16


def seed_to_fraction(seed_hex: str) -> float:
    """Map logged seed material to a uniform fraction in [0, 1)."""
    return int(seed_hex, 16) / float(16 ** len(seed_hex))


def roll_tier(weights: dict[str, float], fraction: float) -> str:
    """Walk the cumulative weight distribution in TIER_ORDER. `fraction` in [0,1)."""
    total = sum(float(weights.get(t, 0)) for t in TIER_ORDER)
    if total <= 0:
        return TIER_ORDER[0]
    acc = 0.0
    point = fraction * total
    for tier in TIER_ORDER:
        acc += float(weights.get(tier, 0))
        if point < acc:
            return tier
    return TIER_ORDER[-1]


def apply_pity(
    tier: str,
    since_epic: int,
    since_legendary: int,
    pity: dict[str, int],
    weights: dict[str, float],
    fraction: float,
) -> tuple[str, str | None]:
    """Force top tiers when thresholds are reached. Counters count rolls
    *since* the last qualifying hit, so the threshold-th roll is the forced
    one (since_x == threshold-1 entering the roll). Returns (tier, pity_hit)
    where pity_hit is None for natural rolls."""
    leg_at = int(pity.get("legendary", 90))
    epic_at = int(pity.get("epic", 20))
    if since_legendary >= leg_at - 1 and tier != "legendary":
        return "legendary", "legendary"
    if since_epic >= epic_at - 1 and tier not in ("epic", "legendary"):
        # Force Epic-or-better, split by the tiers' relative live weights.
        w_epic = float(weights.get("epic", 4))
        w_leg = float(weights.get("legendary", 1))
        total = w_epic + w_leg
        forced = "legendary" if total > 0 and fraction * total >= w_epic else "epic"
        return forced, "epic"
    return tier, None


def next_pity(tier: str, since_epic: int, since_legendary: int) -> tuple[int, int]:
    """Counter transitions after a roll resolves to `tier`."""
    if tier == "legendary":
        return 0, 0
    if tier == "epic":
        return 0, since_legendary + 1
    return since_epic + 1, since_legendary + 1


def pick_available_tier(tier: str, populated: set[str]) -> str | None:
    """The rolled tier, or the nearest populated tier below it (never above —
    protects top-tier scarcity). None if nothing at-or-below is populated;
    callers decide the failsafe."""
    idx = TIER_ORDER.index(tier)
    for t in reversed(TIER_ORDER[: idx + 1]):
        if t in populated:
            return t
    return None


def effective_streak(streak_days: int, last_scan_day: date | None, today: date) -> int:
    """A streak is alive only if the player scanned today or yesterday."""
    if last_scan_day is None or today - last_scan_day > timedelta(days=1):
        return 0
    return streak_days


def next_streak(streak_days: int, last_scan_day: date | None, today: date) -> int:
    """Streak transition when a scan happens on `today`."""
    if last_scan_day == today:
        return max(streak_days, 1)
    if last_scan_day == today - timedelta(days=1):
        return streak_days + 1
    return 1


def daily_allowance(
    streak_days: int, last_scan_day: date | None, today: date, cfg: dict
) -> int:
    """Scans granted at the daily reset. Streak bonus requires the streak to
    still be alive at grant time."""
    alive = effective_streak(streak_days, last_scan_day, today)
    if alive >= int(cfg.get("streak_min_days", 7)):
        return int(cfg.get("streak_amount", 4))
    return int(cfg.get("base", 3))


def dupe_flux(tier: str, cfg: dict) -> int:
    return int(cfg.get(tier, 0))


def income_count_multiplier(count: int, cfg: dict) -> float:
    """Diminishing duplicate income: first copy is full yield, then a bounded
    partial bonus for extra copies."""
    count = max(0, int(count))
    if count <= 0:
        return 0.0
    bonus = float(cfg.get("duplicate_bonus", 0.25))
    cap = int(cfg.get("duplicate_bonus_cap", 4))
    return 1.0 + min(max(count - 1, 0), cap) * bonus


def income_level_multiplier(level: int, cfg: dict) -> float:
    level = max(1, int(level))
    bonus = float(cfg.get("income_bonus_per_level", 0.5))
    return 1.0 + (level - 1) * bonus


def daily_income_for_card(tier: str, count: int, cfg: dict, level: int = 1) -> float:
    by_tier = cfg.get("daily_by_tier", {})
    return (
        float(by_tier.get(tier, 0))
        * income_count_multiplier(count, cfg)
        * income_level_multiplier(level, cfg)
    )


def upgrade_cost(tier: str, current_level: int, cfg: dict) -> int:
    base = int(cfg.get("cost_by_tier", {}).get(tier, 0))
    return base * max(1, int(current_level))


def can_upgrade_card(count: int, level: int, cfg: dict) -> tuple[bool, str | None]:
    max_level = int(cfg.get("max_level", 5))
    if level >= max_level:
        return False, "max_level"
    if count <= level:
        return False, "needs_duplicate"
    return True, None


TOTAL_CATEGORIES = 7  # the globe's category count (incl. AI)

_COUNTRY_BADGES = ((10, "countries-10"), (25, "countries-25"), (50, "countries-50"))


def earned_badges(
    distinct_categories: int,
    distinct_countries: int,
    distinct_continents: int,
    has_legendary: bool,
) -> set[str]:
    """All badge keys the collection currently satisfies. Callers insert with
    ON CONFLICT DO NOTHING, so re-earning is naturally idempotent."""
    earned: set[str] = set()
    if distinct_categories >= TOTAL_CATEGORIES:
        earned.add("categories-7")
    if distinct_continents >= 5:
        earned.add("continents-5")
    for threshold, key in _COUNTRY_BADGES:
        if distinct_countries >= threshold:
            earned.add(key)
    if has_legendary:
        earned.add("first-legendary")
    return earned
