# syntax=docker/dockerfile:1.7
# API + ingest image. Used by both `api` and `ingest` services in compose —
# they share the codebase; the entrypoint command differs.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# build-essential + libpq-dev are needed for psycopg's C extension build,
# even with the binary wheel (some pgvector adapter paths use libpq headers).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts

# pyproject lists the core deps. fastembed / openai / numpy / pgvector
# are imported by the runtime code but not yet declared as project deps —
# install them explicitly so the image is self-contained.
RUN pip install -e . \
        fastembed \
        openai \
        numpy \
        pgvector

# Pre-download the bge-small embedding model so a cold container's first
# /search isn't a multi-second blocking download. Adds ~80 MB to the image
# in exchange for predictable cold-start latency.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

EXPOSE 8088

CMD ["python", "scripts/serve.py"]
