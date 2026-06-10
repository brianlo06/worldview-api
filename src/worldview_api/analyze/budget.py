"""LLM budget for anomaly synopsis generation.

Tracked separately from /ask, /briefing, and the summarizer (each has its own
gate) per the budget-isolation invariant — a noisy news day triggering many
anomalies must not starve the interactive features. Same in-process advisory
semantics as ask/budget.py; over budget, the synopsis degrades to a template.
"""

from __future__ import annotations

from ..ask.budget import _InteractiveLLMBudget
from ..config import settings

# Module-level singleton for anomaly synopsis calls.
budget = _InteractiveLLMBudget(
    cap_getter=lambda: settings.anomaly_llm_daily_cap,
    rpm_getter=lambda: settings.anomaly_llm_max_rpm,
)
