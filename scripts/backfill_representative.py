"""One-time backfill of clusters.representative_event_id (sql/010).

Runs the standard refresh over the full retention window (3 days, per the
pg_cron prune) so every live cluster gets a pick before the API switches to
the denormalized join. Idempotent; the ingest loop maintains it afterwards.

Usage: python scripts/backfill_representative.py
"""
from __future__ import annotations

import logging
import sys

from worldview_api.cluster.representative import refresh_representatives

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stdout,
)

print(refresh_representatives(window_hours=72))
