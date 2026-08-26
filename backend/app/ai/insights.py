"""AI insight generation.

Turns engine state (regimes, gated signals, risk posture, performance,
model status) into human-readable insight cards for the UI. Fully local
and deterministic — no LLM calls anywhere in the insight path.
"""

from __future__ import annotations

from collections import deque

from app.ai.regime import REGIME_LABELS
from app.core.domain import InsightItem, Regime, Signal


class InsightEngine:
    def __init__(self, gemini_api_key: str = "", anthropic_api_key: str = "", max_items: int = 60, narrative_test_mode: bool = False) -> None:
        # Keys retained for interface compatibility; no LLM calls are made.
        self.gemini_api_key = gemini_api_key
        self.anthropic_api_key = anthropic_api_key
        self.items: deque[InsightItem] = deque(maxlen=max_items)
        self.is_research_mode: bool = True
        self.narrative_test_mode: bool = narrative_test_mode

    def recent(self, limit: int = 30) -> list[InsightItem]:
        return list(self.items)[-limit:]

    def _add(self, item: InsightItem) -> InsightItem:
        self.items.append(item)
        return item

    # ---- generators (called by the engine) ----

    def on_regime_change(self, symbol: str, regime: Regime, details: dict) -> InsightItem:
        pieces = []
        if details.get("er") is not None:
            pieces.append(f"efficiency {details['er']:.2f}")
        if details.get("vol_percentile") is not None:
            pieces.append(f"vol percentile {details['vol_percentile'] * 100:.0f}%")
        severity = "warning" if regime == Regime.VOLATILE else "info"
        return self._add(
            InsightItem(
                kind="regime",
                symbol=symbol,
                title=f"{symbol}: {REGIME_LABELS[regime]}",
                detail=f"Regime shifted to {REGIME_LABELS[regime]} ({', '.join(pieces) or 'warmup'}). "
                f"Strategy weights adjusted.",
                severity=severity,
            )
        )

    def on_signal(self, signal: Signal, gated: bool, gate_reason: str = "") -> InsightItem:
        ml = f"{signal.ml_probability * 100:.0f}%" if signal.ml_probability is not None else "n/a"
        action = signal.action.replace("_", " ")
        if gated:
            title = f"{signal.symbol}: {action} candidate rejected"
            detail = (
                f"{signal.strategy} proposed {action} ({signal.reason}) with ML confidence {ml}, "
                f"but was rejected: {gate_reason}."
            )
            severity = "info"
        else:
            title = f"{signal.symbol}: {action} signal accepted"
            detail = (
                f"{signal.strategy}: {signal.reason}. ML confidence {ml}, "
                f"composite score {signal.score:.2f}." if signal.score is not None else
                f"{signal.strategy}: {signal.reason}. ML confidence {ml}."
            )
            severity = "success"
        return self._add(
            InsightItem(kind="signal", symbol=signal.symbol, title=title, detail=detail, severity=severity)
        )

    def on_risk_event(self, title: str, detail: str, severity: str = "warning") -> InsightItem:
        return self._add(InsightItem(kind="risk", title=title, detail=detail, severity=severity))

    def on_performance(self, stats: dict, day_pnl: float) -> InsightItem:
        pf = stats.get("profit_factor")
        detail = (
            f"Session P&L {day_pnl:+,.2f}. {stats['total_trades']} trades, "
            f"win rate {stats['win_rate']}%"
        )
        if pf is not None:
            detail += f", profit factor {pf}"
        detail += "."
        severity = "success" if day_pnl >= 0 else "warning"
        return self._add(InsightItem(kind="performance", title="Session performance", detail=detail, severity=severity))

    def on_model_status(self, info: dict) -> InsightItem:
        if info.get("trained"):
            title = "ML model active"
            detail = (
                f"Gradient boosting model trained {info.get('trained_at', '?')[:10]} on "
                f"{info.get('samples', '?')} samples (validation AUC {info.get('validation_auc')})."
            )
            severity = "success"
        else:
            title = "Heuristic AI gate active"
            detail = (
                "No trained model found -- signals are gated by a transparent momentum heuristic. "
                "Train a model from the AI panel for a data-driven gate."
            )
            severity = "info"
        return self._add(InsightItem(kind="model", title=title, detail=detail, severity=severity))

