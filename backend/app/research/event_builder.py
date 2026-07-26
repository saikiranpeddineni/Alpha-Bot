"""Pure builders for converting Champion values into immutable shadow events."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from app.core.domain import Signal
from app.research.candidate_identity import canonical_candidate_id, canonical_event_id
from app.research.shadow_event import EvidenceProvenance, ShadowEventType, TradingObservationEvent


def candidate_event_from_signal(
    signal: Signal,
    *,
    final_decision: str,
    rejection_reason: str | None = None,
    features: Mapping[str, float | int | None] | None = None,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    atr: float | None = None,
    rsi: float | None = None,
    meta_probability: float | None = None,
    meta_decision: str | None = None,
    trade_score_decision: str | None = None,
    portfolio_decision: str | None = None,
    risk_decision: str | None = None,
    vix: float | None = None,
    vix_status: str | None = None,
    equity: float | None = None,
    cash: float | None = None,
    buying_power: float | None = None,
    calibrated_expected_net_dollars: float | None = None,
    expected_holding_mins: float | None = None,
    expectancy_confidence: float | None = None,
    expectancy_source: str | None = None,
    provenance: EvidenceProvenance | None = None,
    ordinal: int = 0,
) -> TradingObservationEvent:
    """Build a canonical candidate snapshot without mutating Champion decisions.

    Values are copied exactly from the decision context. Callers must pass ``None``
    when evidence is unavailable; this builder never manufactures ATR, RSI, capital,
    or expectancy fallbacks.
    """
    candidate_id = signal.candidate_id or canonical_candidate_id(
        symbol=signal.symbol,
        strategy=signal.strategy,
        side=signal.side,
        decision_ts=signal.ts,
        ordinal=ordinal,
    )
    event_id = canonical_event_id(candidate_id=candidate_id, event_type=ShadowEventType.CANDIDATE.value,
                                  ts=signal.ts, symbol=signal.symbol)
    return TradingObservationEvent(
        event_type=ShadowEventType.CANDIDATE,
        event_id=event_id,
        candidate_id=candidate_id,
        ts=signal.ts,
        decision_bar_timestamp_utc=signal.decision_bar_timestamp_utc,
        symbol=signal.symbol,
        side=signal.side,
        strategy=signal.strategy,
        price=signal.price,
        open=open_, high=high, low=low, close=close,
        atr=atr, rsi=rsi,
        features=dict(features or {}),
        discovery_source=signal.discovery_source,
        scanner_score=signal.scanner_score,
        scanner_auc=signal.scanner_auc,
        ml_probability=signal.ml_probability,
        ml_decision=signal.ml_gate_decision,
        meta_probability=meta_probability,
        meta_decision=meta_decision,
        trade_score=signal.trade_score,
        trade_score_decision=trade_score_decision,
        portfolio_decision=portfolio_decision,
        risk_decision=risk_decision,
        rejection_reason=rejection_reason,
        champion_final_decision=final_decision,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        regime=signal.regime.value if signal.regime is not None else None,
        vix=vix,
        vix_status=vix_status,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
        calibrated_expected_net_dollars=calibrated_expected_net_dollars,
        expected_holding_mins=expected_holding_mins,
        expectancy_confidence=expectancy_confidence,
        expectancy_source=expectancy_source,
        provenance=provenance or EvidenceProvenance(),
    )


def market_bar_event(
    *,
    symbol: str,
    ts: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    atr: float | None = None,
    rsi: float | None = None,
    provenance: EvidenceProvenance | None = None,
) -> TradingObservationEvent:
    event_id = canonical_event_id(candidate_id=None, event_type=ShadowEventType.MARKET_BAR.value,
                                  ts=ts, symbol=symbol)
    return TradingObservationEvent(
        event_type=ShadowEventType.MARKET_BAR,
        event_id=event_id,
        ts=ts,
        symbol=symbol,
        open=open_, high=high, low=low, close=close,
        atr=atr, rsi=rsi,
        provenance=provenance or EvidenceProvenance(),
    )
