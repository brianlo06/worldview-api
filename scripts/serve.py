"""Run the FastAPI service via uvicorn.

Usage:
    .venv/bin/python scripts/serve.py
"""
import uvicorn


def main() -> None:
    uvicorn.run(
        "worldview_api.api:app",
        host="127.0.0.1",
        port=8088,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
