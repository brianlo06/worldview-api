"""Per-share HTML for GET /s/<id>.

Carries OpenGraph + Twitter Card meta so link crawlers (which don't run JS)
unfurl a real preview, then redirects human browsers into the SPA deep link
that reproduces the shared moment.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode

from ..config import settings
from .store import Share


def _redirect_url(share: Share) -> str:
    base = settings.share_redirect_base.rstrip("/")
    # Game pulls recruit into the game, not the news view.
    if share.kind == "pull":
        return f"{base}/game"
    params = share.params or {}
    if params:
        return f"{base}/?{urlencode(params)}"
    return base + "/"


def _og_title(share: Share) -> str:
    if share.kind == "pull":
        tier = str((share.stats or {}).get("tier", "")).upper()
        prefix = f"{tier} CARD — " if tier else ""
        return f"{prefix}{share.title or 'World Cache'} — WORLDVIEW"
    if share.question:
        return f"“{share.question}” — WORLDVIEW"
    if share.place:
        return f"The view from {share.place} — WORLDVIEW"
    return share.title or "WORLDVIEW — the world right now"


def _og_description(share: Share) -> str:
    if share.kind == "pull":
        tier = str((share.stats or {}).get("tier", "a")).lower()
        return (
            f"I pulled a {tier} card from today's real news. "
            "Scan the globe, collect the world — free daily scans at jarvisworlds.com/game."
        )
    if share.answer:
        return share.answer
    if share.place:
        return f"Live situational awareness for {share.place}. Ask the globe anything."
    return "Ask an AI about anywhere on earth, live."


def render_share_html(share: Share) -> str:
    public = settings.share_public_base.rstrip("/")
    card_url = f"{public}/s/{share.id}/card.png"
    page_url = f"{public}/s/{share.id}"
    redirect = _redirect_url(share)

    title = html.escape(_og_title(share))
    desc = html.escape(_og_description(share))
    redirect_attr = html.escape(redirect, quote=True)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta property="og:type" content="website">
<meta property="og:site_name" content="WORLDVIEW">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{card_url}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="{page_url}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{card_url}">
<meta http-equiv="refresh" content="0; url={redirect_attr}">
<link rel="canonical" href="{redirect_attr}">
</head>
<body style="background:#02040a;color:#cfe6ff;font-family:system-ui,sans-serif">
<p>Opening WORLDVIEW… <a href="{redirect_attr}">Continue</a> if you are not redirected.</p>
<script>location.replace({_js_str(redirect)});</script>
</body>
</html>"""


def _js_str(s: str) -> str:
    # Safe JS string literal for the redirect URL.
    import json

    return json.dumps(s)
