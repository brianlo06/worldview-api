"""Natural-language 'ask the globe' endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from ..schemas import AskRequest, AskResponse, AskResultItem

router = APIRouter()


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, response: Response) -> AskResponse:
    """Natural-language 'ask the globe'. Reuses semantic search over clusters,
    synthesizes a short in-character answer (budget-gated), and degrades to a
    templated answer from the top cluster's summary when the LLM is unavailable
    — never 5xx for LLM/budget reasons.
    """
    from ..ask.answer import answer_question

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
