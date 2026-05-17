"""Pull active weather alerts from NOAA NWS once.

Usage:
    .venv/bin/python scripts/run_weather.py
"""
from worldview_api.ingest.weather import main

if __name__ == "__main__":
    main()
