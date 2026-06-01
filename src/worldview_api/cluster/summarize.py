"""Cluster summarization via an OpenAI-compatible LLM (NVIDIA DeepSeek).

When a cluster grows past a milestone or hasn't been summarized recently,
we regenerate its title + summary from the top-K representative members
(closest to the centroid embedding).

Provider is config-driven (`llm_base_url` / `llm_model` / `llm_api_key`),
defaulting to NVIDIA's hosted DeepSeek. We request JSON mode and validate the
response into the `ClusterSummary` Pydantic model; a tolerant fallback extracts
the first JSON object if a model wraps it in prose. Per-cluster failures return
None so the cluster simply keeps its existing headline.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import openai
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from ..config import settings
from ..db import get_pool

log = logging.getLogger(__name__)

# Default backoff (seconds) when a 429 carries no Retry-After header.
_RATE_LIMIT_BACKOFF_S = 5.0

# Min-interval rate limiter. The NVIDIA free tier caps the account at 40 RPM
# shared across projects; we pace summarizer calls to settings.llm_max_rpm.
# The summarizer loop is strictly sequential/single-threaded, so a single
# monotonic timestamp is sufficient — revisit with a lock if it ever
# parallelizes.
_last_request_monotonic = 0.0


def _pace() -> None:
    """Block until at least 60/llm_max_rpm seconds have passed since the last call."""
    global _last_request_monotonic
    rpm = max(1, settings.llm_max_rpm)
    min_interval = 60.0 / rpm
    wait = _last_request_monotonic + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_monotonic = time.monotonic()


def _retry_after_seconds(err: Exception) -> float | None:
    """Best-effort parse of a Retry-After header (seconds) from a 429 error."""
    resp = getattr(err, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


class ClusterSummary(BaseModel):
    """Structured output for a cluster's title + summary."""

    title: str = Field(
        ...,
        max_length=140,
        description=(
            "A single neutral, factual one-line headline summarizing the cluster. "
            "AP style, no editorializing, no clickbait. Max ~100 characters."
        ),
    )
    summary: str = Field(
        ...,
        max_length=500,
        description=(
            "Two to three sentence neutral summary covering who/what/where/when. "
            "State facts only, no opinion or speculation. Max ~400 characters."
        ),
    )


SYSTEM_PROMPT = """You are a senior wire-service editor. Several articles from \
different outlets cover the same underlying event. Your job is to write a single \
neutral, factual headline and a 2-3 sentence summary that captures what they all \
agree on, in AP wire style.

Headline rules:
- Max ~100 characters.
- Present tense for ongoing situations ("airstrikes continue"); simple past for \
completed events ("airstrikes hit").
- Be specific about who, what, where, when. Names and numbers beat generalities.
- No editorializing. Avoid "shocking", "huge", "stunning", "tragic", "horrific", \
"unprecedented". State the facts; let the reader judge.
- No clickbait. Don't tease the story — state it.
- Refer to people by their last name + role on first mention ("President Smith", \
"Senator Jones"). Use full names only when context is unfamiliar.
- Use country-of-action, not country-of-source.

Summary rules:
- 2-3 sentences, max ~400 characters total.
- Sentence 1: the core event (who did what, where, when).
- Sentence 2: the most relevant context, casualty/impact number, or response.
- Sentence 3 (optional): a salient detail like an official quote, follow-on \
action, or noted disagreement among sources.
- If sources disagree on key facts, note it explicitly ("reports vary on the \
death toll", "officials dispute the cause"). Do not pick a side.
- If the underlying story is analysis or commentary rather than breaking news, \
open the summary with "Analysis:" or "Report:".

Style examples — bad vs good:

BAD: "Stunning new revelations rock political world as Senator's secret meetings \
exposed"
GOOD: "Senator Smith met privately with foreign officials twice last month, three \
sources say"

BAD: "Tragic floods devastate American heartland in shocking blow"
GOOD: "Indiana flooding kills 7, displaces 2,400 as White River crests above 18 \
feet"

BAD: "Tech giant suffers massive earnings catastrophe"
GOOD: "Apple shares fall 8% in after-hours trading after Q4 revenue misses \
analyst estimates"

BAD: "Israel pounds Lebanon yet again in latest devastating strikes"
GOOD: "Israeli airstrikes hit seven sites in southern Lebanon, killing 12 \
including 2 children"

When members come from one outlet only (cluster size 1), still neutralize the \
phrasing — strip clickbait, opinion words, and excessive adjectives. Do not \
invent details that aren't in any of the provided articles. If the geographic \
location is ambiguous, pick the most-cited one and don't speculate.

Output format:
Respond with ONLY a single JSON object and nothing else — no markdown fences, \
no commentary:
{"title": "<the headline>", "summary": "<the 2-3 sentence summary>"}
"""


def _get_client() -> OpenAI:
    if not settings.llm_api_key:
        raise RuntimeError(
            "LLM_API_KEY is not set — add it to worldview-api/.env"
        )
    # max_retries=0: the SDK's built-in retries fire rapidly and bypass our
    # _pace() throttle, which would blow the shared 40-RPM budget. We do our
    # own paced single retry on 429 instead.
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        max_retries=0,
    )


def _parse_summary(content: str) -> ClusterSummary | None:
    """Validate the model output into ClusterSummary, tolerant of wrapping prose."""
    content = (content or "").strip()
    try:
        return ClusterSummary.model_validate_json(content)
    except ValidationError:
        pass
    # Fallback: pull the first {...} block (handles fences/prose around the JSON).
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return ClusterSummary.model_validate_json(content[start : end + 1])
        except ValidationError:
            return None
    return None


def _select_pending_clusters(limit: int) -> list[tuple]:
    """Pick clusters that should be (re-)summarized.

    Triggers:
      - Never summarized (and event_count >= 2)
      - event_count grew >= 25% since last summarization
      - Summarized >6h ago (refresh)
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_count, summarized_at_count
            FROM clusters
            WHERE event_count >= 2
              AND last_seen > NOW() - INTERVAL '48 hours'
              AND (
                summarized_at IS NULL
                OR (event_count - coalesce(summarized_at_count, 0))
                   >= GREATEST(1, coalesce(summarized_at_count, 0) * 0.25)
                OR summarized_at < NOW() - INTERVAL '6 hours'
              )
            ORDER BY event_count DESC, last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def _fetch_cluster_members(cluster_id: str, top_n: int) -> list[tuple]:
    """Return the top-N members closest to the cluster centroid."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.title, e.summary, e.source_outlet
            FROM events e
            JOIN clusters c ON c.id = e.cluster_id
            WHERE e.cluster_id = %s AND e.embedding IS NOT NULL
            ORDER BY e.embedding <=> c.centroid_embedding
            LIMIT %s
            """,
            (cluster_id, top_n),
        )
        return cur.fetchall()


def _format_user_prompt(cluster_id: str, members: Sequence[tuple]) -> str:
    lines = [
        f"Cluster ID: {cluster_id}",
        f"Member count: {len(members)} representative article(s)",
        "",
    ]
    for i, (title, summary, outlet) in enumerate(members, start=1):
        lines.append(f"Article {i} ({outlet or 'unknown source'}):")
        lines.append(f"  Title: {title}")
        if summary:
            lines.append(f"  Summary: {summary[:400]}")
        lines.append("")
    lines.append(
        "Produce one neutral title and one 2-3 sentence summary that captures "
        "what these articles agree on."
    )
    return "\n".join(lines)


def _summarize_one(
    client: OpenAI,
    cluster_id: str,
    members: Sequence[tuple],
) -> ClusterSummary | None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _format_user_prompt(cluster_id, members)},
    ]

    def _call(strict: bool):
        _pace()  # rate-limit every request (primary, fallback, and 429 retry)
        kwargs: dict = dict(
            model=settings.llm_model,
            messages=messages,
            temperature=0.3,
            top_p=0.95,
            max_tokens=1024,
            stream=False,
        )
        if strict:
            # JSON mode + disable DeepSeek "thinking". Both are provider-specific;
            # the fallback path drops them and relies on the prompt + _parse_summary.
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["extra_body"] = {"chat_template_kwargs": {"thinking": False}}
        return client.chat.completions.create(**kwargs)

    def _attempt():
        try:
            return _call(strict=True)
        except openai.BadRequestError:
            return _call(strict=False)

    try:
        try:
            response = _attempt()
        except openai.RateLimitError as e:
            # Shared 40-RPM account budget exceeded (often another project).
            # Back off once — honor Retry-After if present — then retry.
            backoff = _retry_after_seconds(e) or _RATE_LIMIT_BACKOFF_S
            log.info("cluster %s: 429 rate-limited, backing off %.1fs", cluster_id, backoff)
            time.sleep(backoff)
            response = _attempt()

        content = response.choices[0].message.content if response.choices else ""
        summary = _parse_summary(content or "")
        if summary is None:
            log.warning("cluster %s: could not parse LLM response: %.200s", cluster_id, content)
            return None

        usage = response.usage
        if usage is not None:
            log.info(
                "summarized %s — prompt=%d completion=%d",
                cluster_id,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            )
        return summary
    except openai.APIStatusError as e:
        log.warning("cluster %s: LLM API %s: %s", cluster_id, e.status_code, e)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("cluster %s: unexpected error: %s", cluster_id, e)
        return None


def summarize_pending(limit: int = 20, members_per_cluster: int = 5) -> dict[str, int | str]:
    if not settings.summarizer_enabled:
        log.info("summarizer disabled via SUMMARIZER_ENABLED=false — skipping")
        return {"status": "disabled", "attempted": 0, "updated": 0}

    pending = _select_pending_clusters(limit=limit)
    if not pending:
        return {"status": "no_pending", "attempted": 0, "updated": 0}

    client = _get_client()
    pool = get_pool()
    updated = 0
    failed = 0

    for cluster_id, event_count, _ in pending:
        members = _fetch_cluster_members(cluster_id, top_n=members_per_cluster)
        if not members:
            continue
        summary = _summarize_one(client, str(cluster_id), members)
        if summary is None:
            failed += 1
            continue

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE clusters
                SET title              = %s,
                    summary            = %s,
                    summarized_at      = NOW(),
                    summarized_at_count = %s
                WHERE id = %s
                """,
                (summary.title, summary.summary, event_count, cluster_id),
            )
            conn.commit()
        updated += 1

    return {
        "status": "ok",
        "attempted": len(pending),
        "updated": updated,
        "failed": failed,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(summarize_pending())


if __name__ == "__main__":
    main()
