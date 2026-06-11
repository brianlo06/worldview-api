"""Per-story holographic scene renders for the briefing.

Each briefing story gets one AI-generated image — a stylized, JARVIS-style
"holographic tactical reconstruction" of the headline — rendered by the
Gemini image model. The frontend projects it as a rotating hologram beside
the globe during playback.

Generation is asynchronous and best-effort: /briefing returns immediately
with predictable /holo/<cluster_id> URLs, a single background thread fills
the files in, and the client polls; until (or unless) a render lands it
shows the story's article photo through the same hologram treatment. Spend
is gated by its own budget per the budget-isolation invariant, and every
failure mode (cap, pace, timeout, refusal, bad key) means "no hologram" —
never an error on the briefing path.

The style is deliberately a translucent monochrome render rather than fake
photojournalism: it reads as a hologram (the point), image models accept it
where they refuse photoreal depictions of real violence, and it can't be
mistaken for an actual photo of the event.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

from ..ask.budget import _InteractiveLLMBudget
from ..config import settings

log = logging.getLogger(__name__)

# Module-level singleton for hologram generation.
budget = _InteractiveLLMBudget(
    cap_getter=lambda: settings.holo_daily_cap,
    rpm_getter=lambda: settings.holo_max_rpm,
)

# Cluster ids a worker thread is currently rendering (or has queued), so
# overlapping briefings don't double-spend on the same story.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()

# Scene FIRST, style after — Flux weights the head of the prompt heavily.
# Leading with style text ("holographic news display...") makes it draw a
# literal display instead of the event; a concrete scene up front followed by
# a render-style tail produces the event as a hologram.
_STYLE = (
    "{scene}. Rendered as a translucent monochrome cyan hologram on a pure "
    "black background — glowing edges, faint wireframe contours, subtle "
    "scanlines, volumetric glow, cinematic lighting, vertical composition. "
    "No text, no captions, no watermarks."
)

_MAX_SCENE_CHARS = 360


def hologram_path(cluster_id: str) -> Path:
    return Path(settings.holo_dir) / f"{cluster_id}.png"


def build_prompt(
    title: str | None,
    summary: str | None,
    category: str | None,
    scene: str | None = None,
) -> str:
    """Hologram render prompt. Prefers the narrator's LLM-written `scene`
    (a literal one-sentence depiction of the event); falls back to cleaned
    title/summary — the narrator's text cleaner keeps wire codes / NWS
    boilerplate out of the render prompt."""
    from .narrate import clean_for_speech

    scene = (scene or "").strip().rstrip(".")
    if not scene:
        parts = [clean_for_speech(title, max_chars=160)]
        body = clean_for_speech(summary, max_chars=200)
        if body and body.lower() != (parts[0] or "").lower():
            parts.append(body)
        scene = " — ".join(p for p in parts if p)
        if category:
            scene = f"({category} news) {scene}"
    return _STYLE.format(scene=scene[:_MAX_SCENE_CHARS])


def _api_key() -> str | None:
    return settings.holo_api_key or settings.llm_api_key


def _call_image_api(prompt: str, cluster_id: str) -> bytes | None:
    if settings.holo_provider == "pollinations":
        return _call_pollinations(prompt, cluster_id)
    return _call_gemini(prompt)


def _call_pollinations(prompt: str, cluster_id: str) -> bytes | None:
    """One GET /image/{prompt} render from gen.pollinations.ai. The seed is
    derived from the cluster id so a retried render converges on the same
    image (their seed range tops out at 2^31-1, hence the modulo). Returns
    image bytes (typically JPEG), or None on a non-image response."""
    seed = int(hashlib.sha1(cluster_id.encode()).hexdigest()[:8], 16) % 2_000_000_000
    url = (
        f"{settings.holo_pollinations_base.rstrip('/')}/image/"
        + urllib.parse.quote(prompt[:1500], safe="")
    )
    headers = {}
    if settings.holo_pollinations_token:
        headers["Authorization"] = f"Bearer {settings.holo_pollinations_token}"
    resp = httpx.get(
        url,
        headers=headers,
        params={
            "model": settings.holo_pollinations_model,
            "width": 832,
            "height": 1024,
            "seed": seed,
        },
        timeout=settings.holo_timeout_s,
        follow_redirects=True,
    )
    resp.raise_for_status()
    if not resp.headers.get("content-type", "").startswith("image/"):
        return None
    return resp.content


def _call_gemini(prompt: str) -> bytes | None:
    """One generateContent call against the native Gemini API (the
    OpenAI-compat layer the text LLMs use doesn't expose image output).
    Returns PNG bytes, or None if the model returned no image (refusal,
    text-only answer)."""
    url = (
        f"{settings.holo_api_base.rstrip('/')}"
        f"/models/{settings.holo_model}:generateContent"
    )
    resp = httpx.post(
        url,
        headers={"x-goog-api-key": _api_key() or ""},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        },
        timeout=settings.holo_timeout_s,
    )
    resp.raise_for_status()
    body = resp.json()
    for cand in body.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    return None


def _generate_one(story: dict) -> bool:
    """Render one story's hologram to disk. Returns True if a file was
    written. Budget-gated; all failures are logged and swallowed."""
    cluster_id = str(story["cluster_id"])
    path = hologram_path(cluster_id)
    if path.is_file():
        return True
    if not budget.try_acquire():
        log.info("holo: budget gate — skipping render for %s", cluster_id)
        return False
    prompt = build_prompt(
        story.get("title"),
        story.get("summary"),
        story.get("category"),
        story.get("scene"),
    )
    try:
        png = _call_image_api(prompt, cluster_id)
    except Exception as e:  # noqa: BLE001 — never propagate past the worker
        log.warning("holo: render failed for %s: %s", cluster_id, e)
        return False
    if not png:
        log.info("holo: model returned no image for %s", cluster_id)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.rename(path)  # atomic: the GET endpoint never sees a partial file
    log.info("holo: rendered %s (%d bytes)", cluster_id, len(png))
    return True


def _prune_old() -> None:
    """Drop renders past the retention window so /tmp doesn't grow forever
    (briefing stories age out of the top list within a day or two anyway)."""
    cutoff = time.time() - settings.holo_max_age_hours * 3600
    root = Path(settings.holo_dir)
    if not root.is_dir():
        return
    for f in root.glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            pass


def _worker(stories: list[dict]) -> None:
    try:
        _prune_old()
        min_interval = 60.0 / max(1, settings.holo_max_rpm)
        first = True
        for story in stories:
            if not first:
                # Pace between calls so the RPM gate in try_acquire (and the
                # provider) never sees a burst; this thread is off the
                # request path, so sleeping here is free.
                time.sleep(min_interval)
            first = False
            _generate_one(story)
    finally:
        with _inflight_lock:
            for story in stories:
                _inflight.discard(str(story["cluster_id"]))


def schedule_generation(stories: list[dict]) -> None:
    """Kick off background rendering for the given briefing stories (dicts
    with cluster_id/title/summary/category). Stories whose render already
    exists or is already queued are skipped. Returns immediately."""
    if not settings.holo_enabled:
        return
    # Only the Gemini provider needs a key; Pollinations is anonymous.
    if settings.holo_provider == "gemini" and not _api_key():
        return
    todo: list[dict] = []
    with _inflight_lock:
        for s in stories:
            cid = str(s["cluster_id"])
            if cid in _inflight or hologram_path(cid).is_file():
                continue
            _inflight.add(cid)
            todo.append(s)
    if not todo:
        return
    threading.Thread(
        target=_worker, args=(todo,), daemon=True, name="holo-render"
    ).start()
