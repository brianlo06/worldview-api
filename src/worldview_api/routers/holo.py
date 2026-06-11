"""Serve briefing hologram renders (see briefing/holo.py)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/holo/{cluster_id}")
def hologram(cluster_id: UUID) -> Response:
    """The cluster's holographic scene render, if it has been generated.

    404 carries no-store: the client polls this URL while the render is in
    flight, and neither the browser nor Cloudflare may cache the miss. The
    path is deliberately extension-less so Cloudflare's default
    cache-by-extension rules never apply to the negative responses.
    """
    from ..briefing.holo import hologram_path

    path = hologram_path(str(cluster_id))
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="hologram not rendered",
            headers={"Cache-Control": "no-store"},
        )
    # One render per cluster id, written once — safe to cache hard.
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
