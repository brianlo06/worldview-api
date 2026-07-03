"""World Notes — public globe notepad.

All routes prefixed /notepad. Registered by worldview_api.routers.__init__.
Fully isolated: no imports from other worldview routers or schemas.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db import get_pool

log = logging.getLogger(__name__)
router = APIRouter(prefix="/notepad", tags=["notepad"])

_RATE_LIMIT_MINUTES = 10
_GLOBE_LIMIT = 500
_MAX_CONTENT = 200
_MAX_NAME = 40

# Allowlist for the reaction UPDATE column. Validated before use in format string.
_REACTION_COLS: dict[str, str] = {
    "heart":     "hearts",
    "celebrate": "celebrations",
    "pray":      "prayers",
    "wave":      "waves",
}

_NOTE_COLS = [
    "id", "content", "author_name", "country_code", "country_name",
    "region", "city", "lat", "lng", "hearts", "celebrations",
    "prayers", "waves", "created_at",
]

# Minimal blocklist — keeps obviously terrible content off without an extra dep.
_BLOCKLIST = frozenset({
    "nigger", "nigga", "faggot", "tranny", "chink", "kike", "spic",
})


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_ip(request: Request) -> str:
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


async def _geolocate(ip: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,countryCode,regionName,city,lat,lon"},
            )
        data = r.json()
        if data.get("status") == "success":
            return {
                "country_name": data.get("country"),
                "country_code": data.get("countryCode"),
                "region":       data.get("regionName"),
                "city":         data.get("city"),
                "lat":          float(data.get("lat") or 0),
                "lng":          float(data.get("lon") or 0),
            }
    except Exception:
        log.warning("geo lookup failed; ip_hash=%s", _hash_ip(ip))
    return {"country_name": None, "country_code": None, "region": None,
            "city": None, "lat": 0.0, "lng": 0.0}


def _is_inappropriate(text: str) -> bool:
    words = set(re.findall(r"\w+", text.lower()))
    return bool(words & _BLOCKLIST)


# ── schemas ───────────────────────────────────────────────────────────────────

class NoteOut(BaseModel):
    id: int
    content: str
    author_name: Optional[str] = None
    country_code: Optional[str] = None
    country_name: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    lat: float
    lng: float
    hearts: int
    celebrations: int
    prayers: int
    waves: int
    created_at: datetime


class NoteIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=_MAX_CONTENT)
    author_name: Optional[str] = Field(None, max_length=_MAX_NAME)


class GeoOut(BaseModel):
    country_name: Optional[str] = None
    country_code: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    lat: float = 0.0
    lng: float = 0.0


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/notes", response_model=list[NoteOut])
def list_notes(since: Optional[datetime] = None):
    """Return up to 500 active notes, newest first. Pass ?since= for incremental polling."""
    cols = ", ".join(_NOTE_COLS)
    pool = get_pool()
    with pool.connection() as conn:
        if since:
            rows = conn.execute(
                f"SELECT {cols} FROM world_notes "
                "WHERE created_at > %s AND expires_at > NOW() AND NOT flagged "
                "ORDER BY created_at DESC LIMIT %s",
                (since, _GLOBE_LIMIT),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM world_notes "
                "WHERE expires_at > NOW() AND NOT flagged "
                "ORDER BY created_at DESC LIMIT %s",
                (_GLOBE_LIMIT,),
            ).fetchall()
    return [NoteOut(**dict(zip(_NOTE_COLS, r))) for r in rows]


@router.post("/notes", response_model=NoteOut, status_code=201)
async def create_note(body: NoteIn, request: Request):
    ip = _get_ip(request)
    ip_hash = _hash_ip(ip)

    if _is_inappropriate(body.content):
        raise HTTPException(status_code=400, detail="Note contains inappropriate content.")

    pool = get_pool()

    # Rate limit check
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT last_post_at FROM notepad_rate_limit WHERE ip_hash = %s",
            (ip_hash,),
        ).fetchone()
    if row:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_RATE_LIMIT_MINUTES)
        if row[0] > cutoff:
            secs = int((row[0] - cutoff).total_seconds())
            raise HTTPException(
                status_code=429,
                detail=f"One note every {_RATE_LIMIT_MINUTES} minutes. Try again in {secs}s.",
            )

    geo = await _geolocate(ip)

    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO notepad_rate_limit (ip_hash, last_post_at) VALUES (%s, NOW()) "
            "ON CONFLICT (ip_hash) DO UPDATE SET last_post_at = NOW()",
            (ip_hash,),
        )
        cols = ", ".join(_NOTE_COLS)
        row = conn.execute(
            f"INSERT INTO world_notes "
            "(content, author_name, country_code, country_name, region, city, lat, lng, ip_hash) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {cols}",
            (
                body.content, body.author_name,
                geo["country_code"], geo["country_name"],
                geo["region"], geo["city"],
                geo["lat"], geo["lng"],
                ip_hash,
            ),
        ).fetchone()
        conn.commit()

    return NoteOut(**dict(zip(_NOTE_COLS, row)))


@router.post("/notes/{note_id}/react", status_code=204)
def react(note_id: int, reaction: str = Query(...)):
    col = _REACTION_COLS.get(reaction)
    if col is None:
        raise HTTPException(status_code=400, detail=f"Unknown reaction. Valid: {list(_REACTION_COLS)}")
    pool = get_pool()
    with pool.connection() as conn:
        # col is from _REACTION_COLS allowlist — safe to interpolate
        result = conn.execute(
            f"UPDATE world_notes SET {col} = {col} + 1 WHERE id = %s AND expires_at > NOW()",
            (note_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Note not found.")
        conn.commit()


@router.post("/notes/{note_id}/flag", status_code=204)
def flag(note_id: int):
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute("UPDATE world_notes SET flagged = TRUE WHERE id = %s", (note_id,))
        conn.commit()


@router.get("/geo", response_model=GeoOut)
async def geo(request: Request):
    """Return geo info for the requesting IP — used by the note form for location preview."""
    ip = _get_ip(request)
    return GeoOut(**await _geolocate(ip))
