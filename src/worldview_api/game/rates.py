"""Rate-table configuration. All tuning values (tier weights, pity, dupe Flux,
daily allowances, mint parameters) live in game_rate_tables rows and are read
per use — an UPDATE takes effect on the next roll, no deploy."""

from __future__ import annotations

from psycopg import Connection

_DEFAULTS: dict[str, dict] = {
    "tier_weights": {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1},
    "pity": {"epic": 20, "legendary": 90},
    "dupe_flux": {"common": 5, "uncommon": 15, "rare": 40, "epic": 100, "legendary": 250},
    "daily_scans": {"base": 3, "streak_min_days": 7, "streak_amount": 4},
    "mint": {"importance_floor": 0.45, "pool_cap": 120, "sparse_min": 40, "legendary_max": 3},
    "card_income": {
        "daily_by_tier": {"common": 1, "uncommon": 2, "rare": 5, "epic": 12, "legendary": 30},
        "duplicate_bonus": 0.25,
        "duplicate_bonus_cap": 4,
        "freshness_hours": 24,
        "freshness_multiplier": 2,
        "accrual_cap_hours": 24,
    },
    "scan_prices": {"bonus": 60, "targeted": 100},
    "card_upgrades": {
        "max_level": 5,
        "income_bonus_per_level": 0.5,
        "cost_by_tier": {"common": 20, "uncommon": 45, "rare": 100, "epic": 220, "legendary": 500},
    },
}


def load_config(conn: Connection, module: str = "scan") -> dict[str, dict]:
    """All rate rows for a module, with hardcoded fallbacks so a missing seed
    row degrades to defaults instead of crashing the scan path."""
    rows = conn.execute(
        "SELECT key, value FROM game_rate_tables WHERE module = %s", (module,)
    ).fetchall()
    cfg = {k: dict(v) for k, v in _DEFAULTS.items()}
    for key, value in rows:
        cfg[key] = value
    return cfg
