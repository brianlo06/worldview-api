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
# fonts-dejavu-core supplies the TrueType faces the share-card renderer
# (share/card.py) draws with — without them Pillow falls back to a bitmap
# default and cards look broken.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts

# pyproject lists the core deps. fastembed / openai / numpy / pgvector are
# imported by the ingest/search code, and pillow by the share-card renderer —
# install them explicitly so the image is self-contained.
#
# Pinned exactly. These were unpinned, so an image rebuild silently moved
# them: pgvector 0.5.0 changed its psycopg loader to return `Vector` instead
# of a numpy array and took out the clustering step with no code change.
# Bump these deliberately, not as a side effect of rebuilding.
RUN pip install -e . \
        fastembed==0.8.0 \
        openai==3.8.0 \
        numpy==2.5.2 \
        pgvector==0.5.0 \
        pillow==12.3.0 \
        piper-tts==1.8.0

# Pre-download the bge-small embedding model so a cold container's first
# /search isn't a multi-second blocking download. Adds ~80 MB to the image
# in exchange for predictable cold-start latency.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# Bake the JARVIS voice (Piper, British male, ~63 MB) so /tts never fetches
# at runtime. Synthesis runs as a subprocess per request — see tts.py.
RUN mkdir -p /app/voices \
 && python -m piper.download_voices en_GB-alan-medium --data-dir /app/voices

EXPOSE 8088

CMD ["python", "scripts/serve.py"]
