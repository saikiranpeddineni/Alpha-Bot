from __future__ import annotations

from dataclasses import dataclass

from .replay_metrics import ExitQuality, score_exit_quality


@dataclass(frozen=True)
class ExitComparison:
    legacy: ExitQuality
    pis: ExitQuality
    realized_delta_r: float
    missed_r_delta: float
    pis_better: bool


def compare_exits(
    *,
    legacy_realized_r: float,
    pis_realized_r: float,
    future_mfe_r: float,
) -> ExitComparison:
    """Compare policies against the same post-entry opportunity set."""
    legacy = score_exit_quality(realized_r=legacy_realized_r, future_mfe_r=future_mfe_r)
    pis = score_exit_quality(realized_r=pis_realized_r, future_mfe_r=future_mfe_r)
    realized_delta = round(pis.realized_r - legacy.realized_r, 4)
    missed_delta = round(pis.missed_r - legacy.missed_r, 4)
    return ExitComparison(
        legacy=legacy,
        pis=pis,
        realized_delta_r=realized_delta,
        missed_r_delta=missed_delta,
        pis_better=realized_delta > 0 and missed_delta < 0,
    )
