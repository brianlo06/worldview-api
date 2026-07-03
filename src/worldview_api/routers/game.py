"""Game spine + SCAN endpoints (jarvisworlds.com/game).

All routes prefixed /game. Player-state routes authenticate via the
X-Player-Token header (see game/identity.py); /game/rates is public.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ..db import get_pool
from ..game import geo, logic, rates as rates_cfg, wallet as wallet_store
from ..game.card_images import image_path
from ..game.identity import mint_token, require_player

log = logging.getLogger(__name__)
router = APIRouter(prefix="/game", tags=["game"])

_MAX_NAME = 40  # world_notes precedent


# ── schemas ──────────────────────────────────────────────────────────────────

class PlayerIn(BaseModel):
    name: Optional[str] = Field(None, max_length=_MAX_NAME)


class WalletOut(BaseModel):
    flux: int
    scans_left: int
    since_epic: int
    since_legendary: int


class PlayerOut(BaseModel):
    player_id: UUID
    name: Optional[str] = None
    streak_days: int
    created_at: datetime
    wallet: WalletOut
    badges: list[str]


class ProvisionOut(PlayerOut):
    token: str  # returned exactly once; only its hash is stored


class NameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=_MAX_NAME)


# ── helpers ──────────────────────────────────────────────────────────────────

def _badges(conn, player_id: UUID) -> list[str]:
    rows = conn.execute(
        "SELECT badge_key FROM game_badges WHERE player_id = %s ORDER BY earned_at",
        (player_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _profile(conn, player_id: UUID) -> tuple[Optional[str], int, datetime]:
    row = conn.execute(
        "SELECT name, streak_days, last_scan_day, created_at "
        "FROM game_players WHERE id = %s",
        (player_id,),
    ).fetchone()
    name, streak_days, last_scan_day, created_at = row
    # Report the live streak: a missed day reads as 0 even before the next scan.
    effective = logic.effective_streak(
        streak_days, last_scan_day, wallet_store.today_utc()
    )
    return name, effective, created_at


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post("/player", response_model=ProvisionOut, status_code=201)
def provision_player(body: PlayerIn | None = None):
    """Mint an anonymous player + wallet atomically. The token in the response
    is shown exactly once and never stored server-side."""
    token, token_hash = mint_token()
    name = body.name if body else None
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        today = wallet_store.today_utc()
        row = conn.execute(
            "INSERT INTO game_players (token_hash, name) VALUES (%s, %s) "
            "RETURNING id, created_at",
            (token_hash, name),
        ).fetchone()
        player_id, created_at = row
        allowance = int(cfg["daily_scans"].get("base", 3))
        conn.execute(
            "INSERT INTO game_wallet (player_id, scans_left, scans_granted_day) "
            "VALUES (%s, %s, %s)",
            (player_id, allowance, today),
        )
        conn.commit()
    return ProvisionOut(
        player_id=player_id,
        token=token,
        name=name,
        streak_days=0,
        created_at=created_at,
        wallet=WalletOut(flux=0, scans_left=allowance, since_epic=0, since_legendary=0),
        badges=[],
    )


@router.get("/player", response_model=PlayerOut)
def get_player(player_id: UUID = Depends(require_player)):
    """Profile + wallet, applying the lazy daily grant."""
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        wallet = wallet_store.lock_and_refresh(conn, player_id, cfg)
        name, streak, created_at = _profile(conn, player_id)
        badges = _badges(conn, player_id)
        conn.commit()
    return PlayerOut(
        player_id=player_id,
        name=name,
        streak_days=streak,
        created_at=created_at,
        wallet=WalletOut(
            flux=wallet["flux"],
            scans_left=wallet["scans_left"],
            since_epic=wallet["since_epic"],
            since_legendary=wallet["since_legendary"],
        ),
        badges=badges,
    )


@router.patch("/player", response_model=PlayerOut)
def set_name(body: NameIn, player_id: UUID = Depends(require_player)):
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        conn.execute(
            "UPDATE game_players SET name = %s WHERE id = %s",
            (body.name.strip() or None, player_id),
        )
        wallet = wallet_store.lock_and_refresh(conn, player_id, cfg)
        name, streak, created_at = _profile(conn, player_id)
        badges = _badges(conn, player_id)
        conn.commit()
    return PlayerOut(
        player_id=player_id,
        name=name,
        streak_days=streak,
        created_at=created_at,
        wallet=WalletOut(
            flux=wallet["flux"],
            scans_left=wallet["scans_left"],
            since_epic=wallet["since_epic"],
            since_legendary=wallet["since_legendary"],
        ),
        badges=badges,
    )


class CardOut(BaseModel):
    card_id: UUID
    headline: str
    summary: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    country: Optional[str] = None
    category: Optional[str] = None
    tier: str
    art_seed: int
    pool_date: date
    image_url: Optional[str] = None
    source_outlet: Optional[str] = None


class ScanOut(BaseModel):
    card: CardOut
    is_dupe: bool
    flux_credit: int
    flux_spent: int = 0
    pity_hit: Optional[str] = None
    new_badges: list[str]
    streak_days: int
    wallet: WalletOut


class ScanIn(BaseModel):
    payment: Literal["free", "flux"] = "free"


class IncomeOut(BaseModel):
    ready: int
    claimed: int
    income_per_day: float
    wallet: WalletOut


class UpgradeOut(BaseModel):
    card_id: UUID
    level: int
    max_level: int
    flux_spent: int
    income_per_day: float
    next_upgrade_cost: Optional[int] = None
    wallet: WalletOut


_CARD_COLS = ("id", "headline", "summary", "lat", "lon", "country",
              "category", "tier", "art_seed", "pool_date", "has_image",
              "source_outlet")


def _card_out(row) -> CardOut:
    d = dict(zip(_CARD_COLS, row))
    card_id = d.pop("id")
    d["card_id"] = card_id
    has_image = d.pop("has_image", False)
    if has_image:
        d["image_url"] = f"/game/cards/{card_id}/image"
    return CardOut(**d)


def _next_utc_midnight() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)


def _income_window(last_claimed: datetime, now: datetime, cfg: dict) -> tuple[datetime, float]:
    cap_hours = float(cfg.get("accrual_cap_hours", 24))
    window_start = max(last_claimed, now - timedelta(hours=cap_hours))
    seconds = max(0.0, (now - window_start).total_seconds())
    return window_start, seconds


def _income_cfg(cfg: dict) -> dict:
    return {**cfg["card_income"], **cfg["card_upgrades"]}


def _income_status(conn, player_id: UUID, cfg: dict, now: datetime | None = None) -> dict:
    """Claimable passive Flux from owned cards. Income is calculated lazily
    over a capped window; no background timers or per-card rows are needed."""
    now = now or datetime.now(timezone.utc)
    last_claimed = conn.execute(
        "SELECT income_claimed_at FROM game_wallet WHERE player_id = %s",
        (player_id,),
    ).fetchone()[0]
    window_start, window_seconds = _income_window(last_claimed, now, cfg)
    if window_seconds <= 0:
        return {"ready": 0, "income_per_day": 0.0, "cards": {}}

    rows = conn.execute(
        "SELECT i.card_id, i.tier, i.count, i.level, i.first_at "
        "FROM game_inventory i WHERE i.player_id = %s",
        (player_id,),
    ).fetchall()
    freshness_hours = float(cfg.get("freshness_hours", 24))
    freshness_multiplier = float(cfg.get("freshness_multiplier", 2))
    card_yields: dict[str, float] = {}
    total_per_day = 0.0
    claimable = 0.0
    for card_id, tier, count, level, first_at in rows:
        daily = logic.daily_income_for_card(tier, count, cfg, level)
        if daily <= 0:
            continue
        total_per_day += daily
        card_yields[str(card_id)] = daily
        start = max(window_start, first_at)
        seconds = max(0.0, (now - start).total_seconds())
        if seconds <= 0:
            continue
        claimable += daily * seconds / 86_400.0
        fresh_until = first_at + timedelta(hours=freshness_hours)
        fresh_seconds = max(0.0, (min(now, fresh_until) - start).total_seconds())
        if fresh_seconds > 0 and freshness_multiplier > 1:
            claimable += daily * (freshness_multiplier - 1) * fresh_seconds / 86_400.0
    return {
        "ready": int(claimable),
        "income_per_day": round(total_per_day, 2),
        "cards": card_yields,
    }


@router.post("/scan", response_model=ScanOut)
def scan(body: ScanIn | None = None, player_id: UUID = Depends(require_player)):
    """Spend one scan → roll a tier (pity-aware) → receive a card from the
    active pool. One transaction: the wallet row lock makes double-submits
    spend exactly once; the pull is durable before the response is sent."""
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        wallet = wallet_store.lock_and_refresh(conn, player_id, cfg)
        body = body or ScanIn()
        flux_spent = 0

        if wallet["scans_left"] <= 0:
            if body.payment == "flux":
                price = int(cfg["scan_prices"].get("bonus", 60))
                if wallet["flux"] < price:
                    conn.commit()  # keep the daily grant even when refusing
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "message": f"Not enough Flux for a bonus scan ({price} needed).",
                            "reset_at": _next_utc_midnight().isoformat(),
                            "bonus_scan_cost": price,
                        },
                    )
                conn.execute(
                    "UPDATE game_wallet SET flux = flux - %s WHERE player_id = %s",
                    (price, player_id),
                )
                wallet["flux"] -= price
                flux_spent = price
            else:
                conn.commit()  # keep the daily grant even when refusing
                raise HTTPException(
                    status_code=429,
                    detail={
                        "message": "No scans left today.",
                        "reset_at": _next_utc_midnight().isoformat(),
                        "bonus_scan_cost": int(cfg["scan_prices"].get("bonus", 60)),
                    },
                )

        today = wallet_store.today_utc()
        pd_row = conn.execute(
            "SELECT max(pool_date) FROM game_card_pool WHERE pool_date <= %s",
            (today,),
        ).fetchone()
        if pd_row is None or pd_row[0] is None:
            raise HTTPException(status_code=503, detail="No card pool minted yet.")
        pool_date = pd_row[0]

        populated = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT tier FROM game_card_pool WHERE pool_date = %s",
                (pool_date,),
            ).fetchall()
        }

        # 32 hex chars of seed material; slices drive the three decisions so
        # the whole pull is re-derivable from the logged seed.
        seed = secrets.token_hex(16)
        natural = logic.roll_tier(cfg["tier_weights"], logic.seed_to_fraction(seed[:16]))
        tier, pity_hit = logic.apply_pity(
            natural, wallet["since_epic"], wallet["since_legendary"],
            cfg["pity"], cfg["tier_weights"], logic.seed_to_fraction(seed[16:24]),
        )
        awarded_tier = logic.pick_available_tier(tier, populated)
        if awarded_tier is None:
            # Pathological pool with nothing at-or-below the roll; take the
            # lowest populated tier rather than failing the scan.
            awarded_tier = min(populated, key=logic.TIER_ORDER.index)
            log.warning("scan: no tier at-or-below %s in pool %s; using %s",
                        tier, pool_date, awarded_tier)

        ids = [r[0] for r in conn.execute(
            "SELECT id FROM game_card_pool WHERE pool_date = %s AND tier = %s "
            "ORDER BY id",
            (pool_date, awarded_tier),
        ).fetchall()]
        card_id = ids[int(logic.seed_to_fraction(seed[24:32]) * len(ids)) % len(ids)]
        card_row = conn.execute(
            f"SELECT {', '.join(_CARD_COLS)} FROM game_card_pool WHERE id = %s",
            (card_id,),
        ).fetchone()

        # Wallet, pity, streak updates
        since_epic, since_legendary = logic.next_pity(
            awarded_tier, wallet["since_epic"], wallet["since_legendary"]
        )
        if flux_spent:
            conn.execute(
                "UPDATE game_wallet SET since_epic = %s, since_legendary = %s "
                "WHERE player_id = %s",
                (since_epic, since_legendary, player_id),
            )
        else:
            conn.execute(
                "UPDATE game_wallet SET scans_left = scans_left - 1, "
                "since_epic = %s, since_legendary = %s WHERE player_id = %s",
                (since_epic, since_legendary, player_id),
            )
        prow = conn.execute(
            "SELECT streak_days, last_scan_day FROM game_players WHERE id = %s",
            (player_id,),
        ).fetchone()
        streak = logic.next_streak(prow[0], prow[1], today)
        conn.execute(
            "UPDATE game_players SET streak_days = %s, last_scan_day = %s "
            "WHERE id = %s",
            (streak, today, player_id),
        )

        # Inventory upsert → dupe detection + Flux credit
        inv_count = conn.execute(
            "INSERT INTO game_inventory (player_id, card_id, tier) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (player_id, card_id) "
            "DO UPDATE SET count = game_inventory.count + 1 "
            "RETURNING count",
            (player_id, card_id, awarded_tier),
        ).fetchone()[0]
        is_dupe = inv_count > 1
        flux_credit = logic.dupe_flux(awarded_tier, cfg["dupe_flux"]) if is_dupe else 0
        if flux_credit:
            conn.execute(
                "UPDATE game_wallet SET flux = flux + %s WHERE player_id = %s",
                (flux_credit, player_id),
            )

        conn.execute(
            "INSERT INTO game_pulls (player_id, module, card_id, tier, roll_seed, "
            " is_dupe, pity_hit) VALUES (%s, 'scan', %s, %s, %s, %s, %s)",
            (player_id, card_id, awarded_tier, seed, is_dupe, pity_hit),
        )

        # Badges from current collection state
        cats, countries, has_leg, country_list = _collection_stats(conn, player_id)
        continents = len({c for c in (geo.continent_of(cc) for cc in country_list) if c})
        earned = logic.earned_badges(cats, countries, continents, has_leg)
        new_badges = []
        for key in sorted(earned):
            cur = conn.execute(
                "INSERT INTO game_badges (player_id, badge_key) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (player_id, key),
            )
            if cur.rowcount:
                new_badges.append(key)

        wrow = conn.execute(
            "SELECT flux, scans_left, since_epic, since_legendary "
            "FROM game_wallet WHERE player_id = %s",
            (player_id,),
        ).fetchone()
        conn.commit()

    return ScanOut(
        card=_card_out(card_row),
        is_dupe=is_dupe,
        flux_credit=flux_credit,
        pity_hit=pity_hit,
        new_badges=new_badges,
        streak_days=streak,
        wallet=WalletOut(flux=wrow[0], scans_left=wrow[1],
                         since_epic=wrow[2], since_legendary=wrow[3]),
        flux_spent=flux_spent,
    )


@router.post("/income/claim", response_model=IncomeOut)
def claim_income(player_id: UUID = Depends(require_player)):
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        wallet_store.lock_and_refresh(conn, player_id, cfg)
        now = datetime.now(timezone.utc)
        status = _income_status(conn, player_id, _income_cfg(cfg), now)
        claimed = int(status["ready"])
        if claimed > 0:
            conn.execute(
                "UPDATE game_wallet SET flux = flux + %s, income_claimed_at = %s "
                "WHERE player_id = %s",
                (claimed, now, player_id),
            )
            conn.execute(
                "INSERT INTO game_income_log (player_id, amount, earned_from, meta) "
                "VALUES (%s, %s, 'cards', %s)",
                (player_id, claimed, Jsonb({"income_per_day": status["income_per_day"]})),
            )
        wrow = conn.execute(
            "SELECT flux, scans_left, since_epic, since_legendary "
            "FROM game_wallet WHERE player_id = %s",
            (player_id,),
        ).fetchone()
        refreshed = _income_status(conn, player_id, _income_cfg(cfg), now)
        conn.commit()
    return IncomeOut(
        ready=int(refreshed["ready"]),
        claimed=claimed,
        income_per_day=float(refreshed["income_per_day"]),
        wallet=WalletOut(flux=wrow[0], scans_left=wrow[1],
                         since_epic=wrow[2], since_legendary=wrow[3]),
    )


@router.post("/cards/{card_id}/upgrade", response_model=UpgradeOut)
def upgrade_card(card_id: UUID, player_id: UUID = Depends(require_player)):
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        wallet = wallet_store.lock_and_refresh(conn, player_id, cfg)
        upgrade_cfg = cfg["card_upgrades"]
        income_cfg = _income_cfg(cfg)
        row = conn.execute(
            "SELECT tier, count, level FROM game_inventory "
            "WHERE player_id = %s AND card_id = %s FOR UPDATE",
            (player_id, card_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Card not in collection.")
        tier, count, level = row
        allowed, reason = logic.can_upgrade_card(count, level, upgrade_cfg)
        if not allowed:
            if reason == "needs_duplicate":
                detail = "Pull another copy of this card to unlock the next level."
            else:
                detail = "Card is already at max level."
            conn.commit()
            raise HTTPException(status_code=400, detail=detail)
        cost = logic.upgrade_cost(tier, level, upgrade_cfg)
        if wallet["flux"] < cost:
            conn.commit()
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"Not enough Flux for upgrade ({cost} needed).",
                    "upgrade_cost": cost,
                },
            )

        new_level = level + 1
        conn.execute(
            "UPDATE game_wallet SET flux = flux - %s WHERE player_id = %s",
            (cost, player_id),
        )
        conn.execute(
            "UPDATE game_inventory SET level = %s "
            "WHERE player_id = %s AND card_id = %s",
            (new_level, player_id, card_id),
        )
        wrow = conn.execute(
            "SELECT flux, scans_left, since_epic, since_legendary "
            "FROM game_wallet WHERE player_id = %s",
            (player_id,),
        ).fetchone()
        next_allowed, _ = logic.can_upgrade_card(count, new_level, upgrade_cfg)
        next_cost = logic.upgrade_cost(tier, new_level, upgrade_cfg) if next_allowed else None
        income_per_day = round(
            logic.daily_income_for_card(tier, count, income_cfg, new_level), 2
        )
        conn.commit()
    return UpgradeOut(
        card_id=card_id,
        level=new_level,
        max_level=int(upgrade_cfg.get("max_level", 5)),
        flux_spent=cost,
        income_per_day=income_per_day,
        next_upgrade_cost=next_cost,
        wallet=WalletOut(flux=wrow[0], scans_left=wrow[1],
                         since_epic=wrow[2], since_legendary=wrow[3]),
    )


@router.get("/cards/{card_id}/image")
def card_image(card_id: UUID):
    path = image_path(card_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Card image not cached.")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _collection_stats(conn, player_id: UUID) -> tuple[int, int, bool, list[str]]:
    """(distinct categories, distinct countries, owns a legendary, country list)."""
    row = conn.execute(
        "SELECT count(DISTINCT p.category) FILTER (WHERE p.category IS NOT NULL), "
        "       count(DISTINCT p.country)  FILTER (WHERE p.country IS NOT NULL), "
        "       bool_or(i.tier = 'legendary') "
        "FROM game_inventory i JOIN game_card_pool p ON p.id = i.card_id "
        "WHERE i.player_id = %s",
        (player_id,),
    ).fetchone()
    countries = [r[0] for r in conn.execute(
        "SELECT DISTINCT p.country FROM game_inventory i "
        "JOIN game_card_pool p ON p.id = i.card_id "
        "WHERE i.player_id = %s AND p.country IS NOT NULL",
        (player_id,),
    ).fetchall()]
    return row[0] or 0, row[1] or 0, bool(row[2]), countries


@router.get("/collection")
def collection(player_id: UUID = Depends(require_player)):
    """Everything the collection browser needs in one response."""
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
        rows = conn.execute(
            f"SELECT {', '.join('p.' + c for c in _CARD_COLS)}, "
            "       i.count, i.level, i.first_at "
            "FROM game_inventory i JOIN game_card_pool p ON p.id = i.card_id "
            "WHERE i.player_id = %s ORDER BY i.first_at DESC",
            (player_id,),
        ).fetchall()
        badges = _badges(conn, player_id)
        pulls = conn.execute(
            "SELECT count(*) FROM game_pulls WHERE player_id = %s", (player_id,)
        ).fetchone()[0]
        upgrade_cfg = cfg["card_upgrades"]
        income = _income_status(conn, player_id, _income_cfg(cfg))

    cards = []
    tiers: dict[str, int] = {}
    categories: set[str] = set()
    countries: set[str] = set()
    has_legendary = False
    for r in rows:
        card = _card_out(r[: len(_CARD_COLS)])
        cards.append({
            **card.model_dump(),
            "count": r[len(_CARD_COLS)],
            "level": r[len(_CARD_COLS) + 1],
            "first_at": r[len(_CARD_COLS) + 2],
            "income_per_day": income["cards"].get(str(card.card_id), 0),
            "max_level": int(upgrade_cfg.get("max_level", 5)),
            "upgrade_cost": (
                logic.upgrade_cost(card.tier, r[len(_CARD_COLS) + 1], upgrade_cfg)
                if logic.can_upgrade_card(
                    r[len(_CARD_COLS)], r[len(_CARD_COLS) + 1], upgrade_cfg
                )[0]
                else None
            ),
            "can_upgrade": logic.can_upgrade_card(
                r[len(_CARD_COLS)], r[len(_CARD_COLS) + 1], upgrade_cfg
            )[0],
        })
        tiers[card.tier] = tiers.get(card.tier, 0) + 1
        if card.category:
            categories.add(card.category)
        if card.country:
            countries.add(card.country)
        has_legendary = has_legendary or card.tier == "legendary"

    continents = {c for c in (geo.continent_of(cc) for cc in countries) if c}
    return {
        "cards": cards,
        "badges": badges,
        "summary": {
            "total_cards": len(cards),
            "total_pulls": pulls,
            "by_tier": tiers,
            "categories": sorted(categories),
            "categories_total": logic.TOTAL_CATEGORIES,
            "countries": sorted(countries),
            "continents": sorted(continents),
            "has_legendary": has_legendary,
            "income_ready": income["ready"],
            "income_per_day": income["income_per_day"],
        },
    }


@router.get("/rates")
def published_rates():
    """Public drop-rate transparency: live tier weights + pity thresholds."""
    pool = get_pool()
    with pool.connection() as conn:
        cfg = rates_cfg.load_config(conn)
    weights = cfg["tier_weights"]
    total = sum(float(v) for v in weights.values()) or 1.0
    return {
        "tiers": {t: round(100.0 * float(weights.get(t, 0)) / total, 2)
                  for t in logic.TIER_ORDER},
        "pity": cfg["pity"],
        "daily_scans": cfg["daily_scans"],
        "dupe_flux": cfg["dupe_flux"],
        "card_income": cfg["card_income"],
        "scan_prices": cfg["scan_prices"],
        "card_upgrades": cfg["card_upgrades"],
    }
