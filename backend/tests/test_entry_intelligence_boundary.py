from datetime import UTC, datetime

import pandas as pd

from app.core.clock import MarketClock
from app.core.domain import Regime, Signal, SignalAction, TradeRecord
from app.config import RiskConfig
from app.risk.manager import RiskManager
from app.strategies.entry_intelligence import assess_catalyst_conviction, assess_entry_context
from app.strategies.pipeline import SignalPipeline


def _bars(close: float = 100.0, atr: float = 1.0) -> pd.DataFrame:
    idx = pd.date_range("2026-08-11 14:00", periods=20, freq="min", tz="UTC")
    values = [close - 10 + i * 0.5 for i in range(20)]
    return pd.DataFrame(
        {
            "open": values,
            "high": [v + 0.2 for v in values],
            "low": [v - 0.2 for v in values],
            "close": values,
            "atr14": [atr] * 20,
            "ema9": [v + 0.2 for v in values],
            "ema20": values,
        },
        index=idx,
    )


def _signal(side: SignalAction) -> Signal:
    return Signal(
        symbol="TEST",
        action=side,
        confidence=0.7,
        strategy="momentum",
        reason="test",
        price=109.5,
        mover_bias="loser" if side == SignalAction.ENTER_LONG else "gainer",
        rs_score=0.40 if side == SignalAction.ENTER_LONG else 0.60,
        regime=Regime.RANGE,
    )


def test_context_boundary_requires_multiple_independent_conflicts():
    assessment = assess_entry_context(_signal(SignalAction.ENTER_LONG), _bars(), Regime.RANGE)
    assert assessment.allowed is False
    assert len(assessment.reasons) >= 2


def test_context_boundary_does_not_ban_confirmed_mover_continuations():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.mover_bias = "gainer"
    sig.rs_score = 0.70
    assessment = assess_entry_context(sig, _bars(), Regime.TREND_UP)
    assert assessment.allowed is True


def test_exceptional_material_catalyst_is_recognized_from_existing_evidence():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.mover_bias = "gainer"
    sig.rs_score = 0.70
    sig.news_context_available = True
    sig.news_direction = "bullish"
    sig.news_confidence = 0.90
    sig.news_catalyst = "earnings"
    sig.news_age_seconds = 120
    exceptional, strength, evidence = assess_catalyst_conviction(sig, _bars())
    assert exceptional is True
    assert 0.0 < strength <= 1.0
    assert evidence["catalyst_material"] is True
    assert evidence["catalyst_fresh"] is True
    assert evidence["catalyst_supportive"] is True
    assert evidence["catalyst_confirmations"] >= 2


def test_extreme_move_without_catalyst_is_not_rescued():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.mover_bias = "gainer"
    sig.rs_score = 0.70
    exceptional, strength, evidence = assess_catalyst_conviction(sig, _bars())
    assert exceptional is False
    assert strength == 0.0
    assert evidence["catalyst_material"] is False


def test_adverse_material_catalyst_is_not_treated_as_positive_conviction():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.mover_bias = "gainer"
    sig.rs_score = 0.70
    sig.news_context_available = True
    sig.news_direction = "bearish"
    sig.news_confidence = 0.90
    sig.news_catalyst = "financing"
    sig.news_age_seconds = 120
    exceptional, strength, evidence = assess_catalyst_conviction(sig, _bars())
    assert exceptional is False
    assert strength == 0.0
    assert evidence["catalyst_supportive"] is False


def test_safety_decision_market_state_alias_is_canonical():
    from app.core.domain import SafetyDecision

    decision = SafetyDecision(
        system_state="NORMAL",
        market_risk_state="CAUTION",
        allow_new_entries=True,
        risk_multiplier=1.0,
        allowed_strategies=["momentum"],
        require_exit_only=False,
    )
    assert decision.market_state == "CAUTION"


def test_signal_carries_context_evidence_fields():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.news_sentiment = -0.4
    sig.context_conflict_count = 1
    assert sig.mover_bias == "loser"
    assert sig.rs_score == 0.40
    assert sig.news_sentiment == -0.4


def test_risk_boundary_can_record_failed_thesis_without_fixed_timer():
    risk = RiskManager(RiskConfig(symbol_cooldown_minutes=0), MarketClock(always_open=True))
    now = datetime.now(UTC)
    trade = TradeRecord(
        symbol="TEST",
        side="long",
        qty=10,
        entry_price=100,
        exit_price=99,
        entry_at=now,
        exit_at=now,
        pnl=-10,
        pnl_pct=-1,
        strategy="momentum",
        metadata={"regime": "range", "mover_bias": "gainer"},
    )
    risk.on_trade_closed(trade, now=now)
    sig = _signal(SignalAction.ENTER_LONG)
    sig.regime = Regime.RANGE
    sig.mover_bias = "gainer"
    decision = risk.is_entry_permitted(sig, now=now)
    assert decision.approved is False
    assert "same-thesis" in decision.reason
