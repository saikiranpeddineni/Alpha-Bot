from __future__ import annotations

from app.core.logging import applog


def get_adaptive_ml_threshold(base_threshold: float, exp_info: dict) -> float:
    """Adjusts the ML confidence gate threshold dynamically based on context performance memory."""
    trades = exp_info.get("trades_count", 0)
    expectancy = exp_info.get("shrunk_expectancy", 0.0)

    # Require minimum statistical sample size of 10 to activate adaptation
    if trades >= 10:
        if expectancy > 0.3:
            adjusted = base_threshold - 0.02
            applog.info(
                "adaptive",
                f"High local context expectancy (+{expectancy:.4f}R) detected. Lowering ML threshold from {base_threshold:.3f} to {adjusted:.3f}"
            )
            return max(0.45, adjusted)
        elif expectancy < -0.1:
            adjusted = base_threshold + 0.03
            applog.info(
                "adaptive",
                f"Negative local context expectancy ({expectancy:.4f}R) detected. Raising ML threshold from {base_threshold:.3f} to {adjusted:.3f}"
            )
            return min(0.60, adjusted)

    return base_threshold


def get_adaptive_specialist_min_auc(strategy: str, regime_key: str, rs_score: float | None) -> float:
    """Adjusts the Specialist validation minimum AUC required on a tick basis."""
    # Default Specialist AUC required threshold is 0.53
    base_auc = 0.53

    # Case 1: Reversion strategies in Range regimes (high probability edge) -> Fail-open slightly
    if strategy in ("vwap_reversion", "rsi_reversion") and regime_key == "RANGE":
        return 0.50

    # Case 2: Momentum breakout strategies with weak relative strength -> Tighten verification
    if strategy in ("bb_squeeze", "orb", "momentum") and rs_score is not None and rs_score < 0.4:
        return 0.56

    return base_auc


def get_adaptive_signal_score(base_score: float, exp_info: dict) -> float:
    """Adjusts the entry signal score based on historical context performance memory."""
    trades = exp_info.get("trades_count", 0)
    expectancy = exp_info.get("shrunk_expectancy", 0.0)

    if trades >= 10:
        # Scale score: expectancy > 0 increases score (boosts allocation priority)
        # Expectancy < 0 decreases score (deprioritizes or filters out allocation)
        multiplier = 1.0 + expectancy
        multiplier = min(1.5, max(0.2, multiplier))
        adjusted_score = base_score * multiplier
        applog.info(
            "adaptive",
            f"Scaling signal score via local expectancy ({expectancy:+.4f}R): {base_score:.2f} -> {adjusted_score:.2f}"
        )
        return round(adjusted_score, 2)

    return base_score
