"""Local fastembed worker — embeds events with BAAI/bge-small-en-v1.5 (384 dim).

No API key, no network call per request. fastembed downloads the ONNX model
once on first use (~130MB) and caches it under ~/.cache/fastembed.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..db import get_pool

log = logging.getLogger(__name__)

_MODEL = None
_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_EMBED_DIM = 384


def _get_model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding

        log.info("loading fastembed model %s (%d dim)", _MODEL_NAME, _EMBED_DIM)
        _MODEL = TextEmbedding(model_name=_MODEL_NAME)
    return _MODEL


def _event_text(title: str, summary: str | None) -> str:
    if summary:
        return f"{title}\n\n{summary}"
    return title


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    model = _get_model()
    return [vec.tolist() for vec in model.embed(list(texts))]


def embed_batch_once(batch_size: int = 100) -> dict[str, int | str]:
    """Embed any events still missing an embedding. Returns counts."""
    pool = get_pool()
    total = 0

    while True:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, summary
                FROM events
                WHERE embedding IS NULL
                ORDER BY occurred_at DESC
                LIMIT %s
                """,
                (batch_size,),
            )
            rows = cur.fetchall()

        if not rows:
            break

        texts = [_event_text(title, summary) for _, title, summary in rows]
        vectors = embed_texts(texts)

        with pool.connection() as conn, conn.cursor() as cur:
            for (event_id, _, _), vec in zip(rows, vectors):
                cur.execute(
                    "UPDATE events SET embedding = %s WHERE id = %s",
                    (vec, event_id),
                )
            conn.commit()

        total += len(rows)
        log.info("embedded %d events (running total %d)", len(rows), total)

    return {"status": "ok", "embedded": total}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(embed_batch_once())


if __name__ == "__main__":
    main()
