"""Unified Regime Strategy Eligibility Matrix.

Coordinates broad market risk states (MarketRiskState) and symbol-level
microstructure tape regimes (Regime) into an authoritative, unified
strategy eligibility and posture decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.domain import Regime
from app.risk.market_regime import MarketRiskState


class UnifiedRegimeLabel(str, Enum):
    TRENDING_BULLISH = "TRENDING_BULLISH"
    TRENDING_BEARISH = "TRENDING_BEARISH"
    SIDEWAYS_RANGE = "SIDEWAYS_RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    MARKET_STRESS = "MARKET_STRESS"


@dataclass(frozen=True)
class RegimePosture:
    regime_label: UnifiedRegimeLabel
    allow_long: bool
    allow_short: bool
    risk_scalar: float
    confidence: float
    rationale: str


class RegimeMatrix:
    """Evaluates macro risk state and symbol tape regime into a unified posture."""

    @staticmethod
    def evaluate(
        macro_state: MarketRiskState,
        symbol_regime: Regime,
        atr_percentile: float | None = None,
    ) -> RegimePosture:
        # 1. Market Stress / Panic / Unknown fail-closed
        if macro_state in (MarketRiskState.STRESS, MarketRiskState.PANIC, MarketRiskState.UNKNOWN):
            return RegimePosture(
                regime_label=UnifiedRegimeLabel.MARKET_STRESS,
                allow_long=False,
                allow_short=False,
                risk_scalar=0.0,
                confidence=0.95,
                rationale=f"Macro risk state {macro_state.value}: all entries frozen for capital preservation",
            )

        # 2. High Volatility (ATR percentile > 0.85 or symbol_regime == VOLATILE)
        if (atr_percentile is not None and atr_percentile > 0.85) or symbol_regime == Regime.VOLATILE:
            return RegimePosture(
                regime_label=UnifiedRegimeLabel.HIGH_VOLATILITY,
                allow_long=True,
                allow_short=True,
                risk_scalar=0.50,
                confidence=0.85,
                rationale="Elevated volatility: positions allowed at 0.5x risk scalar with wider stops",
            )

        # 3. Low Volatility (ATR percentile < 0.20)
        if atr_percentile is not None and atr_percentile < 0.20:
            return RegimePosture(
                regime_label=UnifiedRegimeLabel.LOW_VOLATILITY,
                allow_long=True,
                allow_short=False,
                risk_scalar=0.75,
                confidence=0.80,
                rationale="Compressed volatility: mean-reversion blocked, breakout arming active at 0.75x scalar",
            )

        # 4. Trending Bullish (NORMAL/CAUTION + TREND_UP)
        if symbol_regime == Regime.TREND_UP:
            scalar = 1.0 if macro_state == MarketRiskState.NORMAL else 0.80
            return RegimePosture(
                regime_label=UnifiedRegimeLabel.TRENDING_BULLISH,
                allow_long=True,
                allow_short=False,
                risk_scalar=scalar,
                confidence=0.90,
                rationale="Confirmed bullish trend: full long allocation, counter-trend shorts suppressed",
            )

        # 5. Trending Bearish (NORMAL/CAUTION + TREND_DOWN)
        if symbol_regime == Regime.TREND_DOWN:
            scalar = 1.0 if macro_state == MarketRiskState.NORMAL else 0.80
            return RegimePosture(
                regime_label=UnifiedRegimeLabel.TRENDING_BEARISH,
                allow_long=False,
                allow_short=True,
                risk_scalar=scalar,
                confidence=0.90,
                rationale="Confirmed bearish trend: short allocation enabled, counter-trend longs suppressed",
            )

        # 6. Sideways / Range (RANGE or unclassified)
        scalar = 0.75 if macro_state == MarketRiskState.NORMAL else 0.50
        return RegimePosture(
            regime_label=UnifiedRegimeLabel.SIDEWAYS_RANGE,
            allow_long=True,
            allow_short=True,
            risk_scalar=scalar,
            confidence=0.85,
            rationale="Sideways / range tape: mean-reversion favored at 0.75x risk scalar",
        )
