"""Enrich events with real headlines, descriptions, and OG images.

GDELT doesn't carry article headlines, only source URLs. This worker
fetches each URL, parses OpenGraph + standard HTML metadata, and writes
the result back to the events table.

Designed to be safe to re-run: only events where scraped_at IS NULL are
re-attempted, and concurrent requests are bounded by a semaphore.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Iterable

import httpx
from psycopg_pool import ConnectionPool
from urllib.parse import urlparse

from ..db import get_pool
from .gdelt_gkg import _is_brand_only_title

log = logging.getLogger(__name__)

# Regex-based extraction is fragile but adequate here — OpenGraph tags
# follow a predictable shape, and we don't need a strict HTML parser
# for a single-pass enrichment that tolerates failure.
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_DESC = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMG = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TWITTER_TITLE = re.compile(
    r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TWITTER_DESC = re.compile(
    r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TWITTER_IMG = re.compile(
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TITLE_TAG = re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) worldview-bot/0.1"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _first(*patterns: re.Pattern[str], in_: str) -> str | None:
    for p in patterns:
        m = p.search(in_)
        if m:
            return m.group(1)
    return None


def _clean(s: str | None, *, limit: int) -> str | None:
    if not s:
        return None
    text = html.unescape(s).strip()
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or None


def parse_metadata(body: str) -> dict[str, str | None]:
    title = _first(_OG_TITLE, _TWITTER_TITLE, _TITLE_TAG, in_=body)
    desc = _first(_OG_DESC, _TWITTER_DESC, _META_DESC, in_=body)
    img = _first(_OG_IMG, _TWITTER_IMG, in_=body)
    return {
        "title": _clean(title, limit=240),
        "summary": _clean(desc, limit=600),
        "image_url": _clean(img, limit=500),
    }


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    event_id: str,
    url: str,
) -> tuple[str, str, dict[str, str | None]]:
    async with sem:
        try:
            r = await client.get(url, timeout=8.0, follow_redirects=True)
            if r.status_code != 200:
                return event_id, f"http_{r.status_code}", {}
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype.lower():
                return event_id, "not_html", {}
            # Cap body at 200KB — head of doc is enough for og: tags
            text = r.text[:200_000]
            meta = parse_metadata(text)
            if not meta["title"]:
                return event_id, "no_title", {}
            host = (urlparse(url).netloc or "").lower()
            host = host[4:] if host.startswith("www.") else host
            if _is_brand_only_title(meta["title"], host):
                return event_id, "brand_only_title", {}
            return event_id, "ok", meta
        except httpx.TimeoutException:
            return event_id, "timeout", {}
        except Exception as e:  # noqa: BLE001
            return event_id, f"error:{type(e).__name__}", {}


def _select_pending(pool: ConnectionPool, limit: int) -> list[tuple[str, str]]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, url
            FROM events
            WHERE scraped_at IS NULL
              AND url IS NOT NULL
            ORDER BY coalesce(importance, 0) DESC, occurred_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _write_back(
    pool: ConnectionPool,
    results: Iterable[tuple[str, str, dict[str, str | None]]],
) -> tuple[int, int]:
    ok = 0
    dropped = 0
    with pool.connection() as conn, conn.cursor() as cur:
        for event_id, status, meta in results:
            if status == "ok":
                cur.execute(
                    """
                    UPDATE events
                    SET title = %s,
                        summary = %s,
                        image_url = %s,
                        scraped_at = NOW(),
                        scrape_status = 'ok'
                    WHERE id = %s
                    """,
                    (
                        meta.get("title") or "untitled",
                        meta.get("summary"),
                        meta.get("image_url"),
                        event_id,
                    ),
                )
                ok += 1
            else:
                # Enrichment failed, so the title is still the URL-derived
                # placeholder. Drop the event when that placeholder carries no
                # information: either it has no letters (numeric article ID) or
                # it is just the outlet name (humanize_url's fallback for a
                # letterless slug, e.g. title "Aa.Com.Tr" == outlet "aa.com.tr").
                # Readable word-slug placeholders ("Trump Xi Meeting") are kept.
                cur.execute(
                    "DELETE FROM events WHERE id = %s "
                    "AND (title !~ '[[:alpha:]]' OR lower(title) = lower(source_outlet))",
                    (event_id,),
                )
                if cur.rowcount:
                    dropped += 1
                else:
                    cur.execute(
                        """
                        UPDATE events
                        SET scraped_at = NOW(),
                            scrape_status = %s
                        WHERE id = %s
                        """,
                        (status, event_id),
                    )
        conn.commit()
    return ok, dropped


async def enrich_batch(limit: int = 200, concurrency: int = 8) -> dict[str, int | str]:
    pool = get_pool()
    targets = _select_pending(pool, limit)
    if not targets:
        return {"status": "no_pending", "enriched": 0, "attempted": 0}

    sem = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, sem, eid, url) for eid, url in targets)
        )

    enriched, dropped = _write_back(pool, results)
    by_status: dict[str, int] = {}
    for _, status, _ in results:
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "status": "ok",
        "attempted": len(targets),
        "enriched": enriched,
        "dropped": dropped,
        "by_status": by_status,  # type: ignore[dict-item]
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    import os

    limit = int(os.environ.get("ENRICH_LIMIT", "200"))
    concurrency = int(os.environ.get("ENRICH_CONCURRENCY", "8"))
    result = asyncio.run(enrich_batch(limit=limit, concurrency=concurrency))
    print(result)


if __name__ == "__main__":
    main()
