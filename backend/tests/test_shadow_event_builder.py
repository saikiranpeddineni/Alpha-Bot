from datetime import UTC, datetime

import pytest

from app.core.domain import Regime, Signal, SignalAction
from app.research.event_builder import candidate_event_from_signal, market_bar_event
from app.research.shadow_event import EvidenceProvenance, ShadowEventType


def signal(candidate_id="CHAMPION-ID"):
    return Signal(
        symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.8,
        strategy="momentum", reason="test", price=100.0, stop_loss=99.0,
        take_profit=102.0, ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
        candidate_id=candidate_id, ml_probability=0.61, ml_gate_decision="PASS",
        score=0.72, regime=Regime.TREND_UP,
    )


def test_candidate_builder_preserves_existing_candidate_identity():
    sig = signal()
    before = sig.model_dump()
    event = candidate_event_from_signal(sig, final_decision="accepted", features={"rsi14": 54.0})
    assert event.candidate_id == "CHAMPION-ID"
    assert event.trade_score == 72.0
    assert event.features["rsi14"] == 54.0
    assert sig.model_dump() == before


def test_candidate_builder_deterministically_fills_missing_identity_without_mutating_signal():
    sig = Signal(
        symbol="NVDA", action=SignalAction.ENTER_SHORT, confidence=0.8,
        strategy="momentum", reason="test", price=100.0,
        ts=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
    )
    first = candidate_event_from_signal(sig, final_decision="rejected", rejection_reason="ML")
    second = candidate_event_from_signal(sig, final_decision="rejected", rejection_reason="ML")
    assert first.candidate_id == second.candidate_id
    assert sig.candidate_id is None


def test_candidate_builder_copies_exact_market_and_capital_context_without_fallbacks():
    event = candidate_event_from_signal(
        signal(), final_decision="accepted",
        open_=99.8, high=100.4, low=99.6, close=100.0, atr=1.25, rsi=54.2,
        equity=10_000.0, cash=7_500.0, buying_power=30_000.0,
        vix=18.4, vix_status="FRESH",
    )
    assert (event.open, event.high, event.low, event.close) == (99.8, 100.4, 99.6, 100.0)
    assert event.atr == 1.25
    assert event.rsi == 54.2
    assert event.equity == 10_000.0
    assert event.cash == 7_500.0
    assert event.buying_power == 30_000.0
    assert event.vix == 18.4
    assert event.vix_status == "FRESH"


def test_missing_market_and_expectancy_evidence_stays_unavailable():
    event = candidate_event_from_signal(signal(), final_decision="accepted")
    assert event.atr is None
    assert event.rsi is None
    assert event.open is None
    assert event.calibrated_expected_net_dollars is None
    assert event.expectancy_source is None


def test_calibrated_expectancy_requires_strictly_pre_cutoff_provenance():
    with pytest.raises(ValueError):
        candidate_event_from_signal(
            signal(), final_decision="accepted",
            calibrated_expected_net_dollars=12.5,
            expectancy_confidence=0.8,
            expectancy_source="CALIBRATED",
            provenance=EvidenceProvenance(
                calibration_version="v5",
                calibration_cutoff_utc=datetime(2026, 7, 24, 14, 30, tzinfo=UTC),
                calibration_sample_size=500,
            ),
        )


def test_market_bar_builder_has_no_candidate_identity():
    event = market_bar_event(symbol="NVDA", ts=datetime(2026, 7, 24, 14, 31, tzinfo=UTC),
                             open_=100.0, high=101.0, low=99.5, close=100.5)
    assert event.event_type == ShadowEventType.MARKET_BAR
    assert event.candidate_id is None
