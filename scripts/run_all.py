"""Run every ingestion + enrichment + clustering pass once.

Suitable for scheduling via launchd. Order:
    1. GDELT events ingestion
    2. Article-title enrichment (og:title / image)
    3. NOAA NWS weather alerts
    4. Markets (indices + ETFs)
    5. Currencies (FX rates)
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
from worldview_api.cluster import summarize as cluster_summarize
from worldview_api.embed import embed as embed_mod
from worldview_api.ingest import currencies, enrich, gdelt, gdelt_gkg, markets, weather


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
        ("Enrichment",   lambda: asyncio.run(enrich.enrich_batch(limit=300))),
        ("NWS Weather",  lambda: weather.ingest_nws_once()),
        ("Markets",      lambda: markets.ingest_markets_once()),
        ("Currencies",   lambda: currencies.ingest_currencies_once()),
        ("Embedding",    lambda: embed_mod.embed_batch_once()),
        ("Clustering",   lambda: cluster_assign.cluster_assign_once()),
        ("Summarization",lambda: cluster_summarize.summarize_pending(limit=settings.summarizer_batch_size)),
        ("Anomalies",    lambda: anomalies_mod.run_anomalies_once()),
        # Refresh pre-baked answers for popular /ask questions so interactive
        # traffic hits a warm cache instead of the free-tier LLM. Runs last so
        # it reflects the freshest clusters/summaries from this cycle.
        ("Ask prebake",  lambda: ask_prebake.prebake_once()),
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
