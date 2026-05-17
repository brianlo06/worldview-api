"""Pull the latest GDELT GKG (Global Knowledge Graph) window once.

Usage:
    .venv/bin/python scripts/run_gkg.py
"""
from worldview_api.ingest.gdelt_gkg import main

if __name__ == "__main__":
    main()
