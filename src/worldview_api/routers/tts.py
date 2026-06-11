"""Serve synthesized JARVIS voice lines (see worldview_api/tts.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/tts")
def tts(text: str = Query(..., min_length=1, max_length=700)) -> Response:
    """Neural speech for `text`, synthesized on first request and cached.

    Keyed by the full text, so the browser's HTTP cache makes repeat lines
    (briefing replays, the greeting) free. 503 carries no-store and means
    "fall back to browser speech" — never retry-loop on it.
    """
    from ..tts import synthesize

    path = synthesize(text)
    if path is None:
        raise HTTPException(
            status_code=503,
            detail="speech unavailable",
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
