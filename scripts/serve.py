"""Run the FastAPI service via uvicorn.

Access logs (one line per request — the bulk of log volume) go to a
size-bounded RotatingFileHandler rather than stdout, so the launchd /
container stdout sink can't grow unbounded the way it did before
(api.log had reached 182 MB). Startup + error lines still go to stdout
so launchd/Docker captures them normally.

Usage:
    .venv/bin/python scripts/serve.py
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
ACCESS_LOG = str(LOG_DIR / "api-access.log")

LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s %(client_addr)s "%(request_line)s" %(status_code)s',
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
        "access_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "access",
            "filename": ACCESS_LOG,
            "maxBytes": 20 * 1024 * 1024,  # 20 MB
            "backupCount": 5,  # ≤ ~120 MB total, then oldest dropped
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        # Access logs are bounded and kept off stdout entirely.
        "uvicorn.access": {
            "handlers": ["access_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def main() -> None:
    # reload is a dev convenience; default off so production doesn't run the
    # file-watcher / double process. Set WORLDVIEW_RELOAD=1 locally to enable.
    reload = os.environ.get("WORLDVIEW_RELOAD") == "1"
    uvicorn.run(
        "worldview_api.api:app",
        host="127.0.0.1",
        port=8088,
        reload=reload,
        log_config=LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
