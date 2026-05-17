"""Run one enrichment pass over the most-important un-scraped events.

Usage:
    .venv/bin/python scripts/run_enrich.py
    ENRICH_LIMIT=500 .venv/bin/python scripts/run_enrich.py
"""
from worldview_api.ingest.enrich import main

if __name__ == "__main__":
    main()
