"""Persistence for share snapshots (the `shares` table).

A share denormalizes its card fields at creation time so the card + meta stay
valid after the underlying cluster ages out of the active window.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass

from ..db import get_pool
from . import sanitize_text

log = logging.getLogger(__name__)

_SLUG_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_SLUG_LEN = 10
_VALID_KINDS = {"ask", "city", "cluster", "view", "pull"}


@dataclass
class Share:
    id: str
    kind: str
    params: dict
    title: str | None
    place: str | None
    question: str | None
    answer: str | None
    fly_lat: float | None
    fly_lon: float | None
    stats: dict


def _new_slug() -> str:
    return "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(_SLUG_LEN))


def create_share(
    *,
    kind: str,
    params: dict | None = None,
    title: str | None = None,
    place: str | None = None,
    question: str | None = None,
    answer: str | None = None,
    fly_lat: float | None = None,
    fly_lon: float | None = None,
    stats: dict | None = None,
) -> str:
    """Insert a share snapshot and return its short id. Retries on the
    (astronomically unlikely) slug collision."""
    if kind not in _VALID_KINDS:
        kind = "view"
    params = params or {}
    title = sanitize_text(title, 160) or None
    place = sanitize_text(place, 80) or None
    question = sanitize_text(question, 200) or None
    answer = sanitize_text(answer, 400) or None
    stats = stats or {}

    pool = get_pool()
    for _ in range(5):
        slug = _new_slug()
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO shares
                        (id, kind, params, title, place, question, answer,
                         fly_lat, fly_lon, stats)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        slug, kind, json.dumps(params), title, place, question,
                        answer, fly_lat, fly_lon, json.dumps(stats),
                    ),
                )
                conn.commit()
            return slug
        except Exception as e:  # noqa: BLE001 — retry only on uniqueness clashes
            if "shares_pkey" in str(e) or "duplicate key" in str(e):
                continue
            raise
    raise RuntimeError("could not allocate a unique share id")


def get_share(share_id: str) -> Share | None:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, params, title, place, question, answer,
                   fly_lat, fly_lon, stats
            FROM shares WHERE id = %s
            """,
            (share_id,),
        )
        r = cur.fetchone()
    if not r:
        return None
    return Share(
        id=r[0], kind=r[1], params=r[2] or {}, title=r[3], place=r[4],
        question=r[5], answer=r[6], fly_lat=r[7], fly_lon=r[8], stats=r[9] or {},
    )
