from __future__ import annotations

import logging
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import settings
from .db import close_pool, get_pool
from .observability import (
    KNOWN_SOURCES,
    TIMEOUT_RETURNCODE,
    append_to_ingest_log,
    read_ingest_log_tail,
)
from .scoring import is_breaking, tier_where_clause

log = logging.getLogger(__name__)

# Module-level lock: prevents a second cron tick from spawning a parallel
# ingest if a previous one is still running (ingest takes ~3 min, cron
# fires every 15 min — usually safe, but guard anyway).
_INGEST_LOCK = threading.Lock()
_RUN_ALL_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "run_all.py"

# Container/process start time — used by /admin/status to report uptime.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


class AnomalyOut(BaseModel):
    id: UUID
    region_code: str
    started_at: datetime
    last_seen_at: datetime
    peak_rate: float
    baseline_rate: float
    sigma_above: float
    pulse_lat: float | None = None
    pulse_lon: float | None = None
    driver_titles: list[str]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    hours: int = Field(48, ge=1, le=720)
    limit: int = Field(30, ge=1, le=100)
    # below this we treat the result as off-topic
    min_similarity: float = Field(0.45, ge=0.0, le=1.0)


class SearchResultOut(BaseModel):
    cluster_id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    event_count: int
    category: str | None = None
    importance: float | None = None
    similarity: float
    breaking: bool = False
    geo_precision: str | None = None


class ClusterOut(BaseModel):
    """A cluster, surfaced as its representative event (image, headline, URL),
    with cluster context (event_count) for the 'N sources' indicator."""
    id: UUID
    # Representative event fields — picked from the cluster member nearest to
    # the centroid that has an image (or just nearest if none do)
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    # Cluster context
    first_seen: datetime
    last_seen: datetime
    event_count: int
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    category: str | None = None
    importance: float | None = None
    breaking: bool = False
    geo_precision: str | None = None


class ClusterMemberOut(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    occurred_at: datetime
    categories: list[str]


class ClusterDetailOut(ClusterOut):
    members: list[ClusterMemberOut]


class MarketOut(BaseModel):
    symbol: str
    name: str
    city: str
    country_code: str | None = None
    lat: float
    lon: float
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    currency: str | None = None
    updated_at: datetime


class EventOut(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    occurred_at: datetime
    lat: float
    lon: float
    country_code: str | None = None
    city: str | None = None
    categories: list[str]
    importance: float | None = None
    breaking: bool = False
    geo_precision: str | None = None


def _row_to_event(r: tuple) -> EventOut:
    cats = list(r[9]) if r[9] else []
    return EventOut(
        id=r[0],
        title=r[1],
        summary=r[2],
        url=r[3],
        source_outlet=r[4],
        occurred_at=r[5],
        lat=r[6],
        lon=r[7],
        country_code=r[8],
        categories=cats,
        importance=r[10],
        breaking="breaking" in cats,
        image_url=r[11] if len(r) > 11 else None,
        city=r[12] if len(r) > 12 else None,
        geo_precision=r[13] if len(r) > 13 else None,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_pool()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="worldview-api", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health(response: Response) -> dict[str, str]:
    """Liveness + DB reachability. Returns 503 if Postgres is unreachable so
    the load balancer / uptime monitor sees a real failure instead of a
    cheerful 200 while every data endpoint is 500ing."""
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        # Deliberately no exception detail in the body — internals stay in logs.
        response.status_code = 503
        return {"status": "degraded", "db": "unreachable"}
    return {"status": "ok", "db": "ok"}


def _insert_ingest_run_start(skipped_lock_held: bool) -> int | None:
    """Insert a starting `ingest_runs` row and return its id."""
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_runs (started_at, skipped_lock_held)
                VALUES (NOW(), %s)
                RETURNING id
                """,
                (skipped_lock_held,),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
    except Exception:
        log.exception("ingest: failed to insert ingest_runs row")
        return None


def _update_ingest_run_finish(
    row_id: int | None, returncode: int | None, notes: str | None = None,
) -> None:
    if row_id is None:
        return
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingest_runs
                SET finished_at = NOW(), returncode = %s, notes = %s
                WHERE id = %s
                """,
                (returncode, notes, row_id),
            )
            conn.commit()
    except Exception:
        log.exception("ingest: failed to update ingest_runs row %s", row_id)


def _run_ingest_subprocess() -> None:
    """Spawn run_all.py in a child process. Runs in a Starlette threadpool
    via BackgroundTasks so the response returns immediately. Subprocess
    isolation means the ingest's CPU + memory don't fight the API's event
    loop, and a crash in ingest can't take down the API.

    Records each invocation in the `ingest_runs` table and appends the
    subprocess's stdout+stderr to a rotating log file, so /admin/status
    can surface what happened from outside the container.
    """
    if not _INGEST_LOCK.acquire(blocking=False):
        log.warning("ingest: lock held — another run is in flight, skipping")
        row_id = _insert_ingest_run_start(skipped_lock_held=True)
        _update_ingest_run_finish(row_id, returncode=None, notes="lock held; skipped")
        return

    row_id = _insert_ingest_run_start(skipped_lock_held=False)
    started = datetime.now(timezone.utc)
    try:
        log.info("ingest: starting subprocess (%s)", _RUN_ALL_SCRIPT)
        proc = subprocess.run(
            [sys.executable, str(_RUN_ALL_SCRIPT)],
            check=False,
            timeout=600,  # 10 min hard cap; typical run is ~3 min
            capture_output=True,
            text=True,
        )
        log.info("ingest: subprocess finished rc=%d", proc.returncode)
        header = (
            f"=== ingest run #{row_id} ===\n"
            f"started_at: {started.isoformat()}\n"
            f"finished_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"returncode: {proc.returncode}\n"
        )
        append_to_ingest_log(header, proc.stdout or "", proc.stderr or "")
        _update_ingest_run_finish(row_id, returncode=proc.returncode)
    except subprocess.TimeoutExpired as e:
        log.error("ingest: subprocess timed out after 600s — killed")
        # capture_output buffers the partial output on the exception.
        stdout = (e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        stderr = (e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        header = (
            f"=== ingest run #{row_id} TIMED OUT ===\n"
            f"started_at: {started.isoformat()}\n"
            f"finished_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"returncode: {TIMEOUT_RETURNCODE} (timeout)\n"
        )
        append_to_ingest_log(header, stdout, stderr)
        _update_ingest_run_finish(row_id, returncode=TIMEOUT_RETURNCODE, notes="timed out after 600s")
    except Exception as e:
        log.exception("ingest: subprocess failed")
        header = (
            f"=== ingest run #{row_id} FAILED ===\n"
            f"started_at: {started.isoformat()}\n"
            f"finished_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"error: {type(e).__name__}: {e}\n"
        )
        append_to_ingest_log(header, "", str(e))
        _update_ingest_run_finish(row_id, returncode=None, notes=f"exception: {type(e).__name__}: {e}")
    finally:
        _INGEST_LOCK.release()


@app.post("/admin/run-ingest")
def admin_run_ingest(
    background_tasks: BackgroundTasks,
    x_admin_token: str = Header(..., alias="X-Admin-Token"),
) -> dict[str, str]:
    """Trigger one ingest pass. Called by the CF cron worker every 15 min.
    Returns 202 immediately; the actual work runs in a background thread.
    Token-gated — must match settings.ingest_token (set as a wrangler secret).
    """
    if not settings.ingest_token:
        # Defensive: refuse to run if the token isn't configured, otherwise
        # an empty token would accept blank-header requests.
        raise HTTPException(status_code=503, detail="ingest disabled")
    if x_admin_token != settings.ingest_token:
        raise HTTPException(status_code=401, detail="invalid token")
    background_tasks.add_task(_run_ingest_subprocess)
    return {"status": "queued"}


@app.get("/admin/status")
def admin_status(
    x_admin_token: str = Header(..., alias="X-Admin-Token"),
) -> dict:
    """Snapshot of ingest pipeline state. Token-gated; same secret as
    /admin/run-ingest. Read-only.

    Returns container uptime + per-source watermarks (with explicit nulls
    for known sources that have never produced) + the last 10 ingest runs
    + the tail of the captured ingest log. Designed to answer "is ingest
    working right now, and if not, where is it failing?" in one request.
    """
    if not settings.ingest_token:
        raise HTTPException(status_code=503, detail="admin status disabled")
    if x_admin_token != settings.ingest_token:
        raise HTTPException(status_code=401, detail="invalid token")

    now = datetime.now(timezone.utc)
    uptime_seconds = int((now - _PROCESS_STARTED_AT).total_seconds())

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source, last_seen_at FROM source_watermarks"
        )
        wm_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, started_at, finished_at, returncode, skipped_lock_held, notes
            FROM ingest_runs
            ORDER BY started_at DESC
            LIMIT 10
            """
        )
        recent_runs_raw = cur.fetchall()

    watermarks: dict[str, dict] = {}
    seen_in_db = {src: ts for src, ts in wm_rows}
    for src in KNOWN_SOURCES:
        ts = seen_in_db.get(src)
        watermarks[src] = {
            "last_seen_at": ts.isoformat() if ts else None,
        }
    # Include any extra sources that DB has but code doesn't list, so a
    # future ingester not yet wired into KNOWN_SOURCES still shows up.
    for src, ts in seen_in_db.items():
        if src not in watermarks:
            watermarks[src] = {"last_seen_at": ts.isoformat() if ts else None}

    def _run_row(r: tuple) -> dict:
        rid, started, finished, rc, skipped, notes = r
        return {
            "id": rid,
            "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "returncode": rc,
            "skipped_lock_held": skipped,
            "notes": notes,
        }

    recent_runs = [_run_row(r) for r in recent_runs_raw]
    last_run = recent_runs[0] if recent_runs else None

    return {
        "container": {
            "uptime_seconds": uptime_seconds,
            "started_at": _PROCESS_STARTED_AT.isoformat(),
        },
        "watermarks": watermarks,
        "last_run": last_run,
        "recent_runs": recent_runs,
        "log_tail": read_ingest_log_tail(max_lines=200),
    }


@app.get("/anomalies", response_model=list[AnomalyOut])
def anomalies() -> list[AnomalyOut]:
    """Active regions whose recent event rate has spiked past baseline+3σ.

    Each anomaly carries up to 3 driver-cluster titles — the stories actually
    driving the spike — so the frontend can show 'why' without an extra fetch.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id,
                   a.region_code,
                   a.started_at,
                   a.last_seen_at,
                   a.peak_rate,
                   a.baseline_rate,
                   a.sigma_above,
                   a.pulse_lat,
                   a.pulse_lon,
                   coalesce(
                     (
                       SELECT array_agg(c.title ORDER BY c.event_count DESC)
                       FROM clusters c
                       WHERE c.id = ANY(a.driver_cluster_ids)
                     ),
                     '{}'::text[]
                   ) AS driver_titles
            FROM anomalies a
            WHERE a.status = 'active'
              AND a.last_seen_at > NOW() - INTERVAL '2 hours'
            ORDER BY a.sigma_above DESC
            """
        )
        rows = cur.fetchall()
    return [
        AnomalyOut(
            id=r[0],
            region_code=r[1],
            started_at=r[2],
            last_seen_at=r[3],
            peak_rate=r[4],
            baseline_rate=r[5],
            sigma_above=r[6],
            pulse_lat=r[7],
            pulse_lon=r[8],
            driver_titles=(list(r[9]) if r[9] else [])[:3],
        )
        for r in rows
    ]


@app.post("/search", response_model=list[SearchResultOut])
def search(body: SearchRequest) -> list[SearchResultOut]:
    """Embed the query and find nearest clusters by centroid similarity.

    Uses the same fastembed model that embedded the events, so query and
    cluster vectors live in the same space. Filters by `min_similarity`
    so off-topic queries return [] instead of bottom-rank noise.
    """
    q = body.query.strip()
    if not q:
        return []

    # Import lazily so the fastembed model only loads when /search is hit
    import numpy as np

    from .embed.embed import embed_texts

    # pgvector's <=> operator wants a vector-typed operand; np.ndarray adapts
    # correctly via the pgvector psycopg adapter (list[float] would adapt as
    # a double precision[] and the operator wouldn't match).
    query_vec = np.asarray(embed_texts([q])[0], dtype=np.float32)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   e.title,
                   e.summary,
                   e.url,
                   e.image_url,
                   e.source_outlet,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code,
                   e.city,
                   c.event_count,
                   c.primary_category,
                   c.importance_score,
                   1 - (c.centroid_embedding <=> %s) AS similarity,
                   e.geo_precision
            FROM clusters c
            LEFT JOIN LATERAL (
                SELECT *
                FROM events e2
                WHERE e2.cluster_id = c.id
                  AND e2.embedding IS NOT NULL
                ORDER BY
                    -- Prefer the member with the most precise location so the
                    -- cluster dot doesn't sit on a country centroid when a
                    -- sibling row knows the actual city.
                    CASE e2.geo_precision
                        WHEN 'point'   THEN 0
                        WHEN 'city'    THEN 1
                        WHEN 'state'   THEN 2
                        WHEN 'country' THEN 3
                        ELSE 4
                    END ASC,
                    (e2.image_url IS NOT NULL) DESC,
                    e2.embedding <=> c.centroid_embedding ASC
                LIMIT 1
            ) e ON true
            WHERE c.last_seen > NOW() - (%s * INTERVAL '1 hour')
              AND e.id IS NOT NULL
            ORDER BY c.centroid_embedding <=> %s
            LIMIT %s
            """,
            (query_vec, body.hours, query_vec, body.limit),
        )
        rows = cur.fetchall()

    results: list[SearchResultOut] = []
    for r in rows:
        sim = r[13] if r[13] is not None else 0.0
        if sim < body.min_similarity:
            continue
        importance = r[12]
        event_count = r[10]
        breaking = is_breaking(event_count, importance)
        results.append(
            SearchResultOut(
                cluster_id=r[0],
                title=r[1] or "untitled",
                summary=r[2],
                url=r[3],
                image_url=r[4],
                source_outlet=r[5],
                lat=r[6],
                lon=r[7],
                country_code=r[8],
                city=r[9],
                event_count=event_count,
                category=r[11],
                importance=importance,
                similarity=sim,
                breaking=breaking,
                geo_precision=r[14],
            )
        )
    return results


@app.get("/clusters", response_model=list[ClusterOut])
def clusters(
    response: Response,
    hours: int = Query(48, ge=1, le=720),
    min_events: int = Query(1, ge=1, le=100),
    limit: int = Query(500, ge=1, le=5000),
    tier: Literal["all", "notable", "major", "top"] = Query("all"),
) -> list[ClusterOut]:
    # Ingest runs every 15 min; 30s CDN cache lets a public deploy survive
    # bursts without each visit hammering Postgres.
    response.headers["Cache-Control"] = "public, max-age=30"
    """Active clusters surfaced as their representative event.

    For each cluster, pick the member that (a) has an image, and (b) is
    closest to the centroid embedding. The frontend renders that event's
    headline/image/URL, plus an `event_count` indicator showing how many
    other sources are behind the same story.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    tier_sql, tier_params = tier_where_clause(tier)
    tier_clause = f"AND {tier_sql}" if tier_sql else ""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id,
                   e.title,
                   e.summary,
                   e.url,
                   e.image_url,
                   e.source_outlet,
                   c.first_seen,
                   c.last_seen,
                   c.event_count,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code,
                   e.city,
                   c.primary_category,
                   c.importance_score,
                   e.geo_precision
            FROM clusters c
            LEFT JOIN LATERAL (
                SELECT *
                FROM events e2
                WHERE e2.cluster_id = c.id
                  AND e2.embedding IS NOT NULL
                ORDER BY
                    -- Prefer the member with the most precise location so the
                    -- cluster dot doesn't sit on a country centroid when a
                    -- sibling row knows the actual city.
                    CASE e2.geo_precision
                        WHEN 'point'   THEN 0
                        WHEN 'city'    THEN 1
                        WHEN 'state'   THEN 2
                        WHEN 'country' THEN 3
                        ELSE 4
                    END ASC,
                    (e2.image_url IS NOT NULL) DESC,
                    e2.embedding <=> c.centroid_embedding ASC
                LIMIT 1
            ) e ON true
            WHERE c.last_seen >= %s
              AND c.event_count >= %s
              AND e.id IS NOT NULL
              {tier_clause}
            ORDER BY coalesce(c.importance_score, 0) DESC,
                     c.event_count DESC,
                     c.last_seen DESC
            LIMIT %s
            """,
            (since, min_events, *tier_params, limit),
        )
        rows = cur.fetchall()

    out: list[ClusterOut] = []
    for r in rows:
        importance = r[14]
        event_count = r[8]
        breaking = is_breaking(event_count, importance)
        out.append(
            ClusterOut(
                id=r[0],
                title=r[1],
                summary=r[2],
                url=r[3],
                image_url=r[4],
                source_outlet=r[5],
                first_seen=r[6],
                last_seen=r[7],
                event_count=event_count,
                lat=r[9],
                lon=r[10],
                country_code=r[11],
                city=r[12],
                category=r[13],
                importance=importance,
                breaking=breaking,
                geo_precision=r[15],
            )
        )
    return out


@app.get("/clusters/{cluster_id}", response_model=ClusterDetailOut)
def cluster_detail(cluster_id: UUID) -> ClusterDetailOut:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary, first_seen, last_seen, event_count,
                   ST_Y(centroid_location::geometry) AS lat,
                   ST_X(centroid_location::geometry) AS lon,
                   primary_country, primary_category, importance_score
            FROM clusters
            WHERE id = %s
            """,
            (cluster_id,),
        )
        c = cur.fetchone()
        if c is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="cluster not found")

        cur.execute(
            """
            SELECT e.id, e.title, e.summary, e.url, e.image_url, e.source_outlet,
                   e.occurred_at, e.categories
            FROM events e
            JOIN clusters cl ON cl.id = e.cluster_id
            WHERE e.cluster_id = %s AND e.embedding IS NOT NULL
            ORDER BY e.embedding <=> cl.centroid_embedding
            LIMIT 25
            """,
            (cluster_id,),
        )
        members = cur.fetchall()

    importance = c[10]
    breaking = is_breaking(c[5], importance)
    return ClusterDetailOut(
        id=c[0],
        title=c[1],
        summary=c[2],
        first_seen=c[3],
        last_seen=c[4],
        event_count=c[5],
        lat=c[6],
        lon=c[7],
        country_code=c[8],
        category=c[9],
        importance=importance,
        breaking=breaking,
        members=[
            ClusterMemberOut(
                id=m[0],
                title=m[1],
                summary=m[2],
                url=m[3],
                image_url=m[4],
                source_outlet=m[5],
                occurred_at=m[6],
                categories=list(m[7]) if m[7] else [],
            )
            for m in members
        ],
    )


class BriefingStoryOut(BaseModel):
    """One briefing story: its spoken narration plus the fields the client
    needs to fly the globe and render the selection card without a second
    round-trip to /clusters."""
    cluster_id: UUID
    narration: str
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    category: str | None = None
    occurred_at: datetime | None = None


class BriefingResponse(BaseModel):
    intro: str
    stories: list[BriefingStoryOut]
    outro: str
    # "llm" when the narration was synthesized, "fallback" for the cleaned-up
    # no-LLM path (LLM disabled / over budget / failed).
    source: str = "fallback"


@app.post("/briefing", response_model=BriefingResponse)
def briefing(response: Response) -> BriefingResponse:
    """Top-stories briefing as a short, conversational spoken-word script.

    Selects the top N clusters (last 24h, >=2 events, by importance — the same
    selection the client used to do) and rewrites them into natural narration
    via the LLM, degrading to cleaned-up cluster text on any LLM/budget/timeout
    condition. Never 5xx for LLM reasons; an empty selection skips the LLM."""
    from .briefing.narrate import BriefingInput, generate_briefing

    response.headers["Cache-Control"] = "no-store"
    n = settings.briefing_story_count
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   e.title,
                   e.summary,
                   e.url,
                   e.image_url,
                   e.source_outlet,
                   c.last_seen,
                   ST_Y(e.location::geometry) AS lat,
                   ST_X(e.location::geometry) AS lon,
                   e.country_code,
                   e.city,
                   c.primary_category
            FROM clusters c
            LEFT JOIN LATERAL (
                SELECT *
                FROM events e2
                WHERE e2.cluster_id = c.id
                  AND e2.embedding IS NOT NULL
                  AND e2.location IS NOT NULL
                ORDER BY
                    CASE e2.geo_precision
                        WHEN 'point'   THEN 0
                        WHEN 'city'    THEN 1
                        WHEN 'state'   THEN 2
                        WHEN 'country' THEN 3
                        ELSE 4
                    END ASC,
                    (e2.image_url IS NOT NULL) DESC,
                    e2.embedding <=> c.centroid_embedding ASC
                LIMIT 1
            ) e ON true
            WHERE c.last_seen >= %s
              AND c.event_count >= 2
              AND e.id IS NOT NULL
            ORDER BY coalesce(c.importance_score, 0) DESC,
                     c.event_count DESC,
                     c.last_seen DESC
            LIMIT %s
            """,
            (since, n),
        )
        rows = cur.fetchall()

    selected = [
        {
            "id": r[0],
            "title": r[1],
            "summary": r[2],
            "url": r[3],
            "image_url": r[4],
            "source_outlet": r[5],
            "occurred_at": r[6],
            "lat": r[7],
            "lon": r[8],
            "country_code": r[9],
            "city": r[10],
            "category": r[11],
        }
        for r in rows
    ]

    if not selected:
        return BriefingResponse(intro="", stories=[], outro="", source="fallback")

    inputs: list[BriefingInput] = [
        {
            "cluster_id": str(s["id"]),
            "title": s["title"],
            "summary": s["summary"],
            "city": s["city"],
            "country_code": s["country_code"],
        }
        for s in selected
    ]
    script, source = generate_briefing(inputs)
    narration_by_id = {st["cluster_id"]: st["narration"] for st in script["stories"]}

    stories_out = [
        BriefingStoryOut(
            cluster_id=s["id"],
            narration=narration_by_id.get(str(s["id"]), ""),
            title=s["title"],
            summary=s["summary"],
            url=s["url"],
            image_url=s["image_url"],
            source_outlet=s["source_outlet"],
            lat=s["lat"],
            lon=s["lon"],
            country_code=s["country_code"],
            city=s["city"],
            category=s["category"],
            occurred_at=s["occurred_at"],
        )
        for s in selected
    ]
    return BriefingResponse(
        intro=script["intro"], stories=stories_out, outro=script["outro"], source=source
    )


@app.get("/markets", response_model=list[MarketOut])
def markets() -> list[MarketOut]:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, name, city, country_code,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   price, prev_close, change_pct, currency, updated_at
            FROM markets
            ORDER BY change_pct DESC NULLS LAST
            """
        )
        rows = cur.fetchall()
    return [
        MarketOut(
            symbol=r[0],
            name=r[1],
            city=r[2],
            country_code=r[3],
            lat=r[4],
            lon=r[5],
            price=float(r[6]) if r[6] is not None else None,
            prev_close=float(r[7]) if r[7] is not None else None,
            change_pct=r[8],
            currency=r[9],
            updated_at=r[10],
        )
        for r in rows
    ]


@app.get("/events/recent", response_model=list[EventOut])
def events_recent(
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(500, ge=1, le=5000),
    min_importance: float = Query(0.0, ge=0.0, le=1.0),
) -> list[EventOut]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary, url, source_outlet, occurred_at,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   country_code, categories, importance, image_url, city, geo_precision
            FROM events
            WHERE occurred_at >= %s
              AND coalesce(importance, 0) >= %s
            ORDER BY coalesce(importance, 0) DESC, occurred_at DESC
            LIMIT %s
            """,
            (since, min_importance, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


class AskRequest(BaseModel):
    question: str = Field("", max_length=500)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class AskResultItem(BaseModel):
    id: str | None = None
    title: str
    summary: str | None = None
    lat: float | None = None
    lon: float | None = None
    place: str | None = None
    source_outlet: str | None = None
    image_url: str | None = None
    country_code: str | None = None
    city: str | None = None


class AskResponse(BaseModel):
    answer: str
    place: str | None = None
    fly_lat: float | None = None
    fly_lon: float | None = None
    cluster_refs: list[str] = []
    results: list[AskResultItem] = []
    stats: dict = {}
    source: str = "live"


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, response: Response) -> AskResponse:
    """Natural-language 'ask the globe'. Reuses semantic search over clusters,
    synthesizes a short in-character answer (budget-gated), and degrades to a
    templated answer from the top cluster's summary when the LLM is unavailable
    — never 5xx for LLM/budget reasons.
    """
    from .ask.answer import answer_question

    q = (body.question or "").strip()
    if not q and (body.lat is None or body.lon is None):
        raise HTTPException(status_code=422, detail="question or coordinates required")

    result = answer_question(q, body.lat, body.lon)
    # Cache hits / pre-baked answers are stable for longer; freshly computed
    # ones track the ~15-min ingest cycle.
    max_age = 300 if result.source in ("cache", "prebaked") else 120
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    return AskResponse(
        answer=result.answer,
        place=result.place,
        fly_lat=result.fly_lat,
        fly_lon=result.fly_lon,
        cluster_refs=result.cluster_refs,
        results=[AskResultItem(**r) for r in result.results],
        stats=result.stats,
        source=result.source,
    )


class ShareRequest(BaseModel):
    kind: str = Field("view", max_length=20)
    params: dict = {}
    title: str | None = Field(None, max_length=400)
    place: str | None = Field(None, max_length=200)
    question: str | None = Field(None, max_length=500)
    answer: str | None = Field(None, max_length=800)
    fly_lat: float | None = Field(None, ge=-90, le=90)
    fly_lon: float | None = Field(None, ge=-180, le=180)
    stats: dict = {}


class ShareResponse(BaseModel):
    id: str
    url: str


@app.post("/share", response_model=ShareResponse)
def create_share_endpoint(body: ShareRequest) -> ShareResponse:
    """Snapshot the current view/answer and return a short shareable id. The
    card fields are denormalized so the share stays valid after its source
    cluster ages out."""
    from .share.store import create_share

    share_id = create_share(
        kind=body.kind,
        params=body.params,
        title=body.title,
        place=body.place,
        question=body.question,
        answer=body.answer,
        fly_lat=body.fly_lat,
        fly_lon=body.fly_lon,
        stats=body.stats,
    )
    url = f"{settings.share_public_base.rstrip('/')}/s/{share_id}"
    return ShareResponse(id=share_id, url=url)


@app.get("/s/{share_id}", response_class=HTMLResponse)
def share_page(share_id: str) -> Response:
    """Per-share HTML with OpenGraph/Twitter meta for crawlers; redirects human
    browsers into the SPA deep link."""
    from .share.html import render_share_html
    from .share.store import get_share

    share = get_share(share_id)
    if share is None:
        # Unknown/stale id: send humans to the default globe rather than 404.
        return RedirectResponse(url=settings.share_redirect_base.rstrip("/") + "/", status_code=302)
    html_doc = render_share_html(share)
    # Short cache: the card/meta are stable, but allow correction if regenerated.
    return HTMLResponse(content=html_doc, headers={"Cache-Control": "public, max-age=600"})


@app.get("/s/{share_id}/card.png")
def share_card(share_id: str) -> Response:
    """Immutable 1200x630 preview card. Rendered once, cached on disk."""
    from .share.card import get_or_render_card_path
    from .share.store import get_share

    share = get_share(share_id)
    if share is None:
        raise HTTPException(status_code=404, detail="share not found")
    path = get_or_render_card_path(share)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/events", response_model=list[EventOut])
def events_in_viewport(
    south: float = Query(..., ge=-90, le=90),
    west: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(2000, ge=1, le=10000),
) -> list[EventOut]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, summary, url, source_outlet, occurred_at,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   country_code, categories, importance, image_url, city, geo_precision
            FROM events
            WHERE occurred_at >= %s
              AND ST_Intersects(
                    location::geometry,
                    ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                  )
            ORDER BY coalesce(importance, 0) DESC, occurred_at DESC
            LIMIT %s
            """,
            (since, west, south, east, north, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]
