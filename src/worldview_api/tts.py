"""Neural text-to-speech for JARVIS voice lines (GET /tts).

Synthesis runs Piper as a subprocess per request rather than holding the
model in-process: the box has ~475MB of headroom and a resident onnxruntime
session would eat most of it, while a subprocess loads (~2s), synthesizes,
exits, and gives the memory back. A global lock serializes synthesis so at
most one Piper process exists at a time — concurrent requests queue briefly
or give up (the client falls back to browser speech, never an error loop).

Outputs are cached on disk keyed by (voice, length-scale, text), so each
unique line is synthesized exactly once — briefings replay for 20 minutes
and the greeting/UI lines are static, making cache hits the common case.
The briefing router pre-warms its segments in a background thread the same
way holograms are scheduled.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from .ask.budget import _InteractiveLLMBudget
from .config import settings

log = logging.getLogger(__name__)

# Daily-cap gate only — pacing is enforced naturally by the synth lock, and
# briefing warm-ups burst 7 segments back-to-back, so a strict RPM gate
# would knock the briefing's own narration down to the robot voice.
budget = _InteractiveLLMBudget(
    cap_getter=lambda: settings.tts_daily_cap,
    rpm_getter=lambda: 600,
)

# One Piper subprocess at a time (memory bound, not throughput bound).
_synth_lock = threading.Lock()

_warm_inflight: set[str] = set()
_warm_lock = threading.Lock()


def _voice_tag() -> str:
    return f"{Path(settings.tts_voice).stem}:{settings.tts_length_scale}"


def wav_path_for(text: str) -> Path:
    key = hashlib.sha1(f"{_voice_tag()}|{text}".encode()).hexdigest()
    return Path(settings.tts_dir) / f"{key}.wav"


def _run_piper(text: str, out: Path) -> bool:
    """One Piper subprocess: stdin text -> wav file. Returns success."""
    tmp = out.with_suffix(".tmp.wav")
    cmd = [
        sys.executable, "-m", "piper",
        "-m", settings.tts_voice,
        "-f", str(tmp),
        "--length-scale", str(settings.tts_length_scale),
        "--sentence-silence", "0.25",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=text.encode(),
            capture_output=True,
            timeout=settings.tts_timeout_s,
        )
    except subprocess.TimeoutExpired:
        log.warning("tts: piper timed out (%d chars)", len(text))
        tmp.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        log.warning(
            "tts: piper failed rc=%s: %.200s",
            proc.returncode,
            proc.stderr.decode(errors="replace"),
        )
        tmp.unlink(missing_ok=True)
        return False
    tmp.rename(out)  # atomic: readers never see a partial wav
    return True


def synthesize(text: str, lock_timeout_s: float = 20.0) -> Path | None:
    """Return the wav for `text`, synthesizing on a cache miss.

    None means "couldn't" (disabled, over budget, queue too deep, piper
    failed) — the caller degrades, it never errors out the request path.
    """
    text = " ".join(text.split())[: settings.tts_max_chars]
    if not text or not settings.tts_enabled:
        return None
    out = wav_path_for(text)
    if out.is_file():
        return out
    if not budget.try_acquire():
        log.info("tts: daily cap reached — degrading")
        return None
    if not _synth_lock.acquire(timeout=lock_timeout_s):
        log.info("tts: synth queue too deep — degrading")
        return None
    try:
        # Re-check: the request ahead of us may have rendered the same text.
        if out.is_file():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        ok = _run_piper(text, out)
        if ok:
            log.info("tts: %d chars in %.1fs", len(text), time.monotonic() - t0)
        return out if ok else None
    finally:
        _synth_lock.release()


def _prune_old() -> None:
    cutoff = time.time() - settings.tts_max_age_hours * 3600
    root = Path(settings.tts_dir)
    if not root.is_dir():
        return
    for f in root.glob("*.wav"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            pass


def schedule_synthesis(texts: list[str]) -> None:
    """Warm the cache for the given lines, in order, in the background —
    used by /briefing so segment N+1 is ready before the client asks."""
    if not settings.tts_enabled:
        return
    todo: list[str] = []
    with _warm_lock:
        for t in texts:
            t = " ".join((t or "").split())[: settings.tts_max_chars]
            if not t:
                continue
            key = wav_path_for(t).name
            if key in _warm_inflight or wav_path_for(t).is_file():
                continue
            _warm_inflight.add(key)
            todo.append(t)
    if not todo:
        return

    def _worker() -> None:
        try:
            _prune_old()
            for t in todo:
                synthesize(t, lock_timeout_s=60.0)
        finally:
            with _warm_lock:
                for t in todo:
                    _warm_inflight.discard(wav_path_for(t).name)

    threading.Thread(target=_worker, daemon=True, name="tts-warm").start()
