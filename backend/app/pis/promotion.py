from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromotionMetrics:
    trades: int
    expectancy_delta_r: float
    captured_mfe_delta_pct: float
    premature_exit_delta_pct: float
    max_drawdown_delta_r: float
    catastrophic_regressions: int = 0


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    reasons: tuple[str, ...]


def evaluate_promotion(metrics: PromotionMetrics) -> PromotionDecision:
    """Require evidence that PIS improves exits without buying profit via tail risk."""
    reasons: list[str] = []
    if metrics.trades < 200:
        reasons.append("PIS_PROMOTION_INSUFFICIENT_SAMPLE")
    if metrics.expectancy_delta_r <= 0:
        reasons.append("PIS_PROMOTION_EXPECTANCY_NOT_IMPROVED")
    if metrics.captured_mfe_delta_pct <= 0:
        reasons.append("PIS_PROMOTION_MFE_CAPTURE_NOT_IMPROVED")
    if metrics.premature_exit_delta_pct >= 0:
        reasons.append("PIS_PROMOTION_PREMATURE_EXITS_NOT_REDUCED")
    if metrics.max_drawdown_delta_r > 0.5:
        reasons.append("PIS_PROMOTION_DRAWDOWN_REGRESSION")
    if metrics.catastrophic_regressions:
        reasons.append("PIS_PROMOTION_CATASTROPHIC_REGRESSION")
    return PromotionDecision(not reasons, tuple(reasons) or ("PIS_PROMOTION_APPROVED",))
