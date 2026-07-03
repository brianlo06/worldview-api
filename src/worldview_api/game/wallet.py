"""Wallet persistence: lazy daily grant + streak bookkeeping.

The daily reset is lazy — applied the first time the wallet row is touched on
a new UTC day — so there is no scheduled job to miss. Callers must hold the
wallet row lock (SELECT ... FOR UPDATE) when mutating."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from psycopg import Connection

from . import logic

WALLET_COLS = ("flux", "scans_left", "scans_granted_day", "since_epic", "since_legendary")


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def lock_and_refresh(conn: Connection, player_id: UUID, cfg: dict) -> dict:
    """Lock the wallet row and apply the daily grant if this is its first
    touch of the current UTC day. Returns the wallet as a dict (post-grant).
    Must be called inside a transaction."""
    today = today_utc()
    row = conn.execute(
        f"SELECT {', '.join(WALLET_COLS)}, p.streak_days, p.last_scan_day "
        "FROM game_wallet w JOIN game_players p ON p.id = w.player_id "
        "WHERE w.player_id = %s FOR UPDATE OF w",
        (player_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no wallet for player {player_id}")
    wallet = dict(zip(WALLET_COLS, row[: len(WALLET_COLS)]))
    streak_days, last_scan_day = row[len(WALLET_COLS):]

    if wallet["scans_granted_day"] != today:
        # Grant replaces (never adds to) yesterday's remainder.
        allowance = logic.daily_allowance(
            streak_days, last_scan_day, today, cfg["daily_scans"]
        )
        conn.execute(
            "UPDATE game_wallet SET scans_left = %s, scans_granted_day = %s "
            "WHERE player_id = %s",
            (allowance, today, player_id),
        )
        wallet["scans_left"] = allowance
        wallet["scans_granted_day"] = today

    wallet["streak_days"] = streak_days
    wallet["last_scan_day"] = last_scan_day
    return wallet
