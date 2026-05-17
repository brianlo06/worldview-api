"""Cluster any embedded events that don't yet have a cluster_id.

Usage:
    .venv/bin/python scripts/run_cluster.py
"""
from worldview_api.cluster.assign import main

if __name__ == "__main__":
    main()
