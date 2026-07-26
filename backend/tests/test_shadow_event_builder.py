from datetime import UTC, datetime

from app.core.domain import Regime, Signal, SignalAction
from app.research.event_builder import candidate_event_from_signal, market_bar_event
from app.research.shadow_event import ShadowEventType


def test_candidate_builder_preserves_existing_candidate_identity():
    signal = Signal(
        symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.8,
        strategy="momentum", reason="test", price=100.0, stop_loss=99.0,
        take_profit=102.0, ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
        candidate_id="CHAMPION-ID", ml_probability=0.61, ml_gate_decision="PASS",
        score=0.72, regime=Regime.TREND_UP,
    )
    before = signal.model_dump()
    event = candidate_event_from_signal(signal, final_decision="accepted", features={"rsi14": 54.0})
    assert event.candidate_id == "CHAMPION-ID"
    assert event.trade_score == 72.0
    assert event.features["rsi14"] == 54.0
    assert signal.model_dump() == before


def test_candidate_builder_deterministically_fills_missing_identity_without_mutating_signal():
    signal = Signal(
        symbol="NVDA", action=SignalAction.ENTER_SHORT, confidence=0.8,
        strategy="momentum", reason="test", price=100.0,
        ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
    )
    first = candidate_event_from_signal(signal, final_decision="rejected", rejection_reason="ML")
    second = candidate_event_from_signal(signal, final_decision="rejected", rejection_reason="ML")
    assert first.candidate_id == second.candidate_id
    assert signal.candidate_id is None


def test_market_bar_builder_has_no_candidate_identity():
    event = market_bar_event(symbol="NVDA", ts=datetime(2026, 7, 24, 14, 31, tzinfo=UTC),
                             open_=100.0, high=101.0, low=99.5, close=100.5)
    assert event.event_type == ShadowEventType.MARKET_BAR
    assert event.candidate_id is None
