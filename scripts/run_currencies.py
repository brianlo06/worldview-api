"""Pull current FX rates from Frankfurter once.

Usage:
    .venv/bin/python scripts/run_currencies.py
"""
from worldview_api.ingest.currencies import main

if __name__ == "__main__":
    main()
