from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitQuality:
    realized_r: float
    future_mfe_r: float
    captured_mfe_pct: float | None
    missed_r: float
    premature_exit: bool
    winner_became_large_winner: bool


def score_exit_quality(
    *,
    realized_r: float,
    future_mfe_r: float,
    premature_threshold_r: float = 1.0,
    large_winner_r: float = 5.0,
) -> ExitQuality:
    """Measure exit quality without rewarding lookahead in the live policy.

    future_mfe_r is strictly a post-trade research label. It must never be fed
    into PositionBrain or any live decision feature.
    """
    realized = float(realized_r)
    future_mfe = float(future_mfe_r)
    missed = max(0.0, future_mfe - realized)
    captured = None
    if future_mfe > 0:
        captured = round(max(0.0, min(100.0, realized / future_mfe * 100.0)), 4)
    return ExitQuality(
        realized_r=round(realized, 4),
        future_mfe_r=round(future_mfe, 4),
        captured_mfe_pct=captured,
        missed_r=round(missed, 4),
        premature_exit=missed >= premature_threshold_r,
        winner_became_large_winner=realized > 0 and realized < large_winner_r <= future_mfe,
    )
