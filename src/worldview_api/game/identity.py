"""Anonymous player identity.

Tokens are server-minted secrets returned exactly once at provisioning; only
their SHA-256 hex digest is stored. Game endpoints authenticate via the
X-Player-Token header.
"""

from __future__ import annotations

import hashlib
import secrets
from uuid import UUID

from fastapi import Header, HTTPException

from ..db import get_pool


def mint_token() -> tuple[str, str]:
    """Returns (plaintext token, sha256 hex digest)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_player(x_player_token: str = Header(...)) -> UUID:
    """FastAPI dependency: resolve the header to a player id (401 on miss)
    and touch last_seen_at in the same round trip."""
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE game_players SET last_seen_at = NOW() "
            "WHERE token_hash = %s RETURNING id",
            (hash_token(x_player_token),),
        ).fetchone()
        conn.commit()
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown player token.")
    return row[0]
