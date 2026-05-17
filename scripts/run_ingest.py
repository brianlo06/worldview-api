"""Run a single GDELT ingestion pass.

Usage:
    .venv/bin/python scripts/run_ingest.py
"""
from worldview_api.ingest.gdelt import main

if __name__ == "__main__":
    main()
