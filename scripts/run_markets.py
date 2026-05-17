"""Pull current quotes for all tracked market instruments.

Usage:
    .venv/bin/python scripts/run_markets.py
"""
from worldview_api.ingest.markets import main

if __name__ == "__main__":
    main()
