"""Interactive LLM budget for POST /briefing.

Tracked separately from POST /ask and the cluster summarizer (each has its own
gate) so a briefing burst cannot starve interactive answers or ingest-time
summarization, per the budget-isolation invariant. Same in-process, advisory,
single-worker semantics as ask/budget.py — when the cap or pace is exceeded the
endpoint serves the cleaned-up no-LLM fallback rather than blocking or erroring.
"""

from __future__ import annotations

from ..ask.budget import _InteractiveLLMBudget
from ..config import settings

# Module-level singleton for POST /briefing.
budget = _InteractiveLLMBudget(
    cap_getter=lambda: settings.briefing_llm_daily_cap,
    rpm_getter=lambda: settings.briefing_llm_max_rpm,
)
