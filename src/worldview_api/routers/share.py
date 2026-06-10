"""Share-card creation and public share pages."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..config import settings
from ..schemas import ShareRequest, ShareResponse

router = APIRouter()


@router.post("/share", response_model=ShareResponse)
def create_share_endpoint(body: ShareRequest) -> ShareResponse:
    """Snapshot the current view/answer and return a short shareable id. The
    card fields are denormalized so the share stays valid after its source
    cluster ages out."""
    from ..share.store import create_share

    share_id = create_share(
        kind=body.kind,
        params=body.params,
        title=body.title,
        place=body.place,
        question=body.question,
        answer=body.answer,
        fly_lat=body.fly_lat,
        fly_lon=body.fly_lon,
        stats=body.stats,
    )
    url = f"{settings.share_public_base.rstrip('/')}/s/{share_id}"
    return ShareResponse(id=share_id, url=url)


@router.get("/s/{share_id}", response_class=HTMLResponse)
def share_page(share_id: str) -> Response:
    """Per-share HTML with OpenGraph/Twitter meta for crawlers; redirects human
    browsers into the SPA deep link."""
    from ..share.html import render_share_html
    from ..share.store import get_share

    share = get_share(share_id)
    if share is None:
        # Unknown/stale id: send humans to the default globe rather than 404.
        return RedirectResponse(url=settings.share_redirect_base.rstrip("/") + "/", status_code=302)
    html_doc = render_share_html(share)
    # Short cache: the card/meta are stable, but allow correction if regenerated.
    return HTMLResponse(content=html_doc, headers={"Cache-Control": "public, max-age=600"})


@router.get("/s/{share_id}/card.png")
def share_card(share_id: str) -> Response:
    """Immutable 1200x630 preview card. Rendered once, cached on disk."""
    from ..share.card import get_or_render_card_path
    from ..share.store import get_share

    share = get_share(share_id)
    if share is None:
        raise HTTPException(status_code=404, detail="share not found")
    path = get_or_render_card_path(share)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
