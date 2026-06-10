"""Interactive LLM budget for POST /ask.

Tracked separately from the cluster summarizer (cluster/summarize.py has its
own pacer) so an /ask traffic spike cannot starve ingest-time summarization,
per the budget-isolation requirement. In-process and thread-safe; advisory by
design — when the cap or pace is exceeded the caller serves the degraded
(no-LLM) answer rather than blocking or erroring.

Caveat: state is per-process. If the API is ever run with multiple uvicorn
workers each gets its own budget, so the effective cap is cap * workers. The
prod stack runs a single worker; revisit (e.g. a Postgres counter) if that
changes.
"""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Callable

from ..config import settings


class _InteractiveLLMBudget:
    """Daily-cap + RPM-pace gate for an interactive LLM endpoint.

    The cap/rpm are read through callables so a single class can back several
    isolated budgets (e.g. /ask and /briefing), each pointing at its own
    settings, while still picking up live setting changes (used by tests)."""

    def __init__(
        self,
        cap_getter: Callable[[], int],
        rpm_getter: Callable[[], int],
    ) -> None:
        self._cap_getter = cap_getter
        self._rpm_getter = rpm_getter
        self._lock = threading.Lock()
        self._day: date = _utc_today()
        self._count = 0
        self._last_monotonic = 0.0

    def try_acquire(self) -> bool:
        """Reserve one interactive LLM call. Returns False (→ degrade) if the
        daily cap is spent or requests are arriving faster than the RPM pace."""
        cap = max(0, self._cap_getter())
        rpm = max(1, self._rpm_getter())
        min_interval = 60.0 / rpm
        now = time.monotonic()
        with self._lock:
            today = _utc_today()
            if today != self._day:
                self._day = today
                self._count = 0
            if self._count >= cap:
                return False
            # Too soon since the last call — degrade instead of queueing so
            # interactive latency stays bounded under a burst.
            if self._last_monotonic and (now - self._last_monotonic) < min_interval:
                return False
            self._count += 1
            self._last_monotonic = now
            return True

    def stats(self) -> dict[str, int]:
        with self._lock:
            if _utc_today() != self._day:
                return {"spent": 0, "cap": self._cap_getter()}
            return {"spent": self._count, "cap": self._cap_getter()}

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._day = _utc_today()
            self._count = 0
            self._last_monotonic = 0.0


def _utc_today() -> date:
    t = time.gmtime()
    return date(t.tm_year, t.tm_mon, t.tm_mday)


# Module-level singleton for POST /ask.
budget = _InteractiveLLMBudget(
    cap_getter=lambda: settings.ask_llm_daily_cap,
    rpm_getter=lambda: settings.ask_llm_max_rpm,
)
