"""Map GDELT CAMEO codes + signals onto the worldview category typology."""

from __future__ import annotations

CAMEO_ROOT_TO_CATEGORY: dict[str, str] = {
    "01": "politics",  # Public Statement
    "02": "politics",  # Appeal
    "03": "politics",  # Express Intent To Cooperate
    "04": "politics",  # Consult
    "05": "politics",  # Diplomatic Cooperation
    "06": "business",  # Material Cooperation
    "07": "social",    # Provide Aid
    "08": "politics",  # Yield
    "09": "politics",  # Investigate
    "10": "politics",  # Demand
    "11": "politics",  # Disapprove
    "12": "politics",  # Reject
    "13": "conflict",  # Threaten
    "14": "social",    # Protest
    "15": "conflict",  # Exhibit Force Posture
    "16": "politics",  # Reduce Relations
    "17": "conflict",  # Coerce
    "18": "conflict",  # Assault
    "19": "conflict",  # Fight
    "20": "conflict",  # Use Unconventional Mass Violence
}


def cameo_to_category(event_root_code: str | None) -> str:
    if not event_root_code:
        return "politics"
    return CAMEO_ROOT_TO_CATEGORY.get(event_root_code, "politics")


def _float(s: str | None, default: float = 0.0) -> float:
    if not s:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def importance_from_row(row: dict[str, str]) -> float:
    """0..1 score from mentions, intensity, and conflict bonus."""
    base = 0.35
    base += min(0.30, _float(row.get("NumMentions")) / 100.0)
    base += min(0.20, abs(_float(row.get("GoldsteinScale"))) / 20.0)
    if row.get("QuadClass") == "4":  # material conflict
        base += 0.10
    return max(0.0, min(1.0, base))


def breaking_from_row(row: dict[str, str]) -> bool:
    """Material conflict + strongly negative Goldstein → flag as breaking."""
    if row.get("QuadClass") != "4":
        return False
    return _float(row.get("GoldsteinScale")) < -7.0
