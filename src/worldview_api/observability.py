"""Ingest observability — capture + persist subprocess output, record run
metadata, and expose them via the API's /admin/status route.

The ingest subprocess (run_all.py) runs inside a Cloudflare Container with
no external log access. Without these helpers, when ingest stalls we have
no visibility into why. This module is the durable fix:

- subprocess stdout/stderr is captured, scrubbed for secrets, and appended
  to a size-bounded log file on the container's local disk.
- Each invocation records one row in the `ingest_runs` table with start /
  finish / returncode / skipped flag.
- A small set of source-name constants lets /admin/status report every
  known source's last_seen_at even if it has no row in source_watermarks.

Everything here is small, framework-free Python so it's easy to test.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

# Where the captured subprocess log lives. /tmp is on the container's
# ephemeral disk — survives within a container instance lifetime, lost
# across redeploys (which is fine for diagnostic purposes).
INGEST_LOG_PATH = Path("/tmp/ingest.log")

# Hard cap: file is truncated to retain only the most recent half when it
# exceeds this. 2 MiB total, 1 MiB retained on rotation. Keeps disk usage
# trivial without losing the last few runs' detail.
INGEST_LOG_MAX_BYTES = 2 * 1024 * 1024
INGEST_LOG_KEEP_BYTES = 1 * 1024 * 1024

# File-write lock — multiple ingest invocations would normally be serialized
# by the threading.Lock in api.py, but keep this defensive lock so the log
# helper is safe to call concurrently from tests too.
_LOG_LOCK = threading.Lock()

# Sentinel returncode for subprocess.TimeoutExpired. Real subprocess
# returncodes are 0..255 (and negative for signal-killed); we pick -1 as
# the "killed by our timeout" marker. /admin/status callers can map this
# back to "timed out" in the UI.
TIMEOUT_RETURNCODE = -1

# Sources the codebase knows about. /admin/status fills `last_seen_at: null`
# for any source listed here that doesn't yet have a row in source_watermarks.
# Keep in sync with the source identifiers used in
# worldview_api/ingest/*.py (gdelt.ingest_once, gdelt_gkg.ingest_gkg_once,
# weather.ingest_nws_once).
KNOWN_SOURCES: tuple[str, ...] = ("gdelt", "gdelt_gkg", "nws")


# Secret patterns scrubbed before writing to the log. Order matters for
# overlapping matches — most specific first.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # postgres://user:pass@host  →  postgres://[REDACTED]@host
    (
        re.compile(r"(postgres(?:ql)?://)[^@\s/]+:[^@\s/]+(@)", re.IGNORECASE),
        r"\1[REDACTED]\2",
    ),
    # sk-ant-anything  →  sk-ant-[REDACTED]
    (
        re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
        "sk-ant-[REDACTED]",
    ),
    # nvapi-anything (NVIDIA)  →  nvapi-[REDACTED]
    (
        re.compile(r"nvapi-[A-Za-z0-9_\-]+"),
        "nvapi-[REDACTED]",
    ),
    # KEY=VALUE for sensitive env names
    (
        re.compile(
            r"\b(DATABASE_URL|LLM_API_KEY|ANTHROPIC_API_KEY|INGEST_TOKEN)\s*=\s*\S+",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
)


def redact(line: str) -> str:
    """Apply all secret patterns. Returns the line with credentials removed."""
    for pattern, replacement in _SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    return line


def _rotate_if_needed(path: Path) -> None:
    """If `path` is over INGEST_LOG_MAX_BYTES, truncate to keep the last half."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size <= INGEST_LOG_MAX_BYTES:
        return
    # Read the tail bytes and rewrite. Cheap for a 2 MiB cap.
    with path.open("rb") as f:
        f.seek(max(0, size - INGEST_LOG_KEEP_BYTES))
        tail = f.read()
    # Try to start at the next newline boundary so we don't leave a half-line.
    nl = tail.find(b"\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    path.write_bytes(b"--- log rotated ---\n" + tail)


def append_to_ingest_log(header: str, stdout: str, stderr: str) -> None:
    """Append a single run's captured output to the rotating log file.

    `header` is a short metadata block (timestamps, returncode). `stdout`
    and `stderr` are the raw subprocess output — they get redacted before
    being written.
    """
    INGEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    body_lines: list[str] = [header.rstrip(), ""]
    if stdout:
        body_lines.append("--- stdout ---")
        body_lines.extend(redact(line) for line in stdout.splitlines())
    if stderr:
        body_lines.append("--- stderr ---")
        body_lines.extend(redact(line) for line in stderr.splitlines())
    body_lines.append("")  # trailing blank for readability between runs
    body = "\n".join(body_lines) + "\n"

    with _LOG_LOCK:
        with INGEST_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(body)
        _rotate_if_needed(INGEST_LOG_PATH)


def read_ingest_log_tail(max_lines: int = 200) -> list[str]:
    """Return up to the last `max_lines` lines of the ingest log file."""
    if not INGEST_LOG_PATH.exists():
        return []
    with INGEST_LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return [line.rstrip("\n") for line in all_lines[-max_lines:]]
