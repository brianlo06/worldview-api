"""Run every ingestion + enrichment + clustering pass once.

Suitable for scheduling via launchd. Order:
    1. GDELT events ingestion
    2. Article-title enrichment (og:title / image)
    3. NOAA NWS weather alerts
    4. Currencies (FX rates)
    6. Embedding pass (local fastembed)
    7. Cluster assignment (greedy kNN via pgvector)
    8. Cluster summarization (NVIDIA DeepSeek)

Each step is independent — if one fails, the next still runs.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback

from worldview_api.config import settings
from worldview_api.analyze import anomalies as anomalies_mod
from worldview_api.ask import prebake as ask_prebake
from worldview_api.cluster import assign as cluster_assign
from worldview_api.cluster import representative as cluster_representative
from worldview_api.cluster import summarize as cluster_summarize
from worldview_api.embed import embed as embed_mod
from worldview_api.game import mint as game_mint
from worldview_api.ingest import currencies, enrich, gdelt, gdelt_gkg, weather


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("run_all")
    started = time.time()

    steps = [
        ("GDELT events", lambda: gdelt.ingest_once()),
        ("GDELT GKG",    lambda: gdelt_gkg.ingest_gkg_once()),
        # The translingual feed runs ~5% US against the English feed's ~53%,
        # and roughly doubles the article volume per cycle.
        ("GDELT GKG intl", lambda: gdelt_gkg.ingest_gkg_once(translation=True)),
        ("Enrichment",   lambda: asyncio.run(enrich.enrich_batch(limit=300))),
        ("NWS Weather",  lambda: weather.ingest_nws_once()),
        ("Currencies",   lambda: currencies.ingest_currencies_once()),
        ("Embedding",    lambda: embed_mod.embed_batch_once()),
        ("Clustering",   lambda: cluster_assign.cluster_assign_once()),
        # Re-pick each active cluster's representative member after centroids
        # moved / members joined; the API reads this denormalized id instead
        # of running the expensive per-cluster pick on every request.
        ("Representatives", lambda: cluster_representative.refresh_representatives()),
        ("Summarization",lambda: cluster_summarize.summarize_pending(limit=settings.summarizer_batch_size)),
        ("Anomalies",    lambda: anomalies_mod.run_anomalies_once()),
        # Refresh pre-baked answers for popular /ask questions so interactive
        # traffic hits a warm cache instead of the free-tier LLM. Runs last so
        # it reflects the freshest clusters/summaries from this cycle.
        ("Ask prebake",  lambda: ask_prebake.prebake_once()),
        # Mint the day's game card pool (SCAN module). Idempotent per UTC
        # day — the first cycle after midnight mints, the rest no-op. Runs
        # after summarization so cards snapshot the freshest summaries.
        ("Game mint",    lambda: game_mint.mint_if_needed()),
        # Retention is enforced in the database by a pg_cron job
        # (sql/008_pg_cron.sql), not here — the ingest loop no longer prunes.
    ]

    for name, fn in steps:
        log.info("=== %s ===", name)
        try:
            result = fn()
            log.info("%s: %s", name.lower(), result)
        except Exception:
            log.error("%s failed:\n%s", name, traceback.format_exc())

    log.info("done in %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
