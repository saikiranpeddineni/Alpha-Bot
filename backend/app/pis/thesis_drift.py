from __future__ import annotations

from .models import ThesisState


def classify_thesis_drift(
    entry_health: float | None,
    current_health: float | None,
    *,
    strengthening_delta: float = 7.5,
    weakening_delta: float = -10.0,
    broken_health: float = 35.0,
) -> tuple[float | None, ThesisState]:
    if entry_health is None or current_health is None:
        return None, ThesisState.UNKNOWN
    drift = round(float(current_health) - float(entry_health), 4)
    if current_health <= broken_health:
        return drift, ThesisState.BROKEN
    if drift >= strengthening_delta:
        return drift, ThesisState.STRENGTHENING
    if drift <= weakening_delta:
        return drift, ThesisState.WEAKENING
    return drift, ThesisState.STABLE
