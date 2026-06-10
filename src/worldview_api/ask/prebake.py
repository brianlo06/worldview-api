"""Pre-bake answers for high-volume general-public questions, once per ingest
cycle, so they cost ZERO per-user LLM calls and are always warm in ask_cache.

Called from scripts/run_all.py after summarization. Uses batch synthesis
(use_budget=False) so it draws on the same once-per-cycle LLM allowance as the
summarizer rather than the interactive /ask budget. Each entry degrades to a
templated answer if the LLM is unavailable — never raises.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..db import get_pool
from . import answer as A
from .places import DEFAULT_TOP_COUNTRIES

log = logging.getLogger(__name__)


def _top_countries(limit: int = 5) -> list[str]:
    """Countries with the most recent cluster activity — what people are most
    likely to ask about right now. Falls back to a fixed salient set."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT primary_country, COUNT(*) AS n
            FROM clusters
            WHERE last_seen > NOW() - (%s * INTERVAL '1 hour')
              AND primary_country IS NOT NULL
            GROUP BY primary_country
            ORDER BY n DESC
            LIMIT %s
            """,
            (settings.ask_search_hours, limit),
        )
        rows = cur.fetchall()
    live = [r[0] for r in rows if r[0]]
    # Pad with the default salient set so we always pre-bake a useful spread.
    for cc in DEFAULT_TOP_COUNTRIES:
        if cc not in live:
            live.append(cc)
    return live[:limit]


def _bake_one(key: str, question: str, stories: list[A.Story]) -> None:
    if not stories:
        # Still store a warm "nothing notable" so the lookup is a hit, not a miss
        # that triggers live retrieval under load.
        result = A.AnswerResult(
            answer="Nothing major is breaking there at the moment.",
            source="prebaked",
            stats={"stories": 0},
        )
        A.cache_put(key, question, result, source="prebaked")
        return
    synth = A._synthesize(question, stories, use_budget=False)
    text = synth if synth is not None else A._degraded_answer(question, stories)
    result = A._build_result(stories, text, source="prebaked")
    A.cache_put(key, question, result, source="prebaked")


def prebake_once() -> dict[str, int]:
    """Refresh the pre-baked answer set. Idempotent — overwrites by key."""
    baked = 0

    # Global intents (guaranteed cache-hit keys).
    top = A.retrieve_top_clusters(settings.ask_search_limit)
    _bake_one("intent:biggest_story", "biggest story right now", top)
    baked += 1
    _bake_one("intent:world_ok", "is the world okay today", top)
    baked += 1

    # Per-top-country.
    for cc in _top_countries(limit=5):
        stories = A.retrieve_top_by_country(cc, settings.ask_search_limit)
        _bake_one(f"intent:country:{cc}", f"what's happening in {cc}", stories)
        baked += 1

    log.info("prebake: refreshed %d popular answers", baked)
    return {"baked": baked}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
    print(prebake_once())


if __name__ == "__main__":
    main()
