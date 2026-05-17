from __future__ import annotations

from psycopg import Connection
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def _configure_connection(conn: Connection) -> None:
    """Run once per new pooled connection. Registers pgvector type adapters
    so we can read/write numpy arrays and list[float] as pgvector values."""
    from pgvector.psycopg import register_vector

    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=8,
            kwargs={"autocommit": False},
            configure=_configure_connection,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
