from datetime import UTC, datetime, timedelta

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.entry_intelligence import assess_entry_context, assess_thesis_continuity


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
    assert assessment.allowed is True  # Re-calibrated bounds allow 2 conflicts if score > 0.65
    assert len(assessment.reasons) >= 2


def test_context_boundary_does_not_ban_confirmed_mover_continuations():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.mover_bias = "gainer"
    sig.rs_score = 0.70
    assessment = assess_entry_context(sig, _bars(), Regime.TREND_UP)
    assert assessment.allowed is True


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
    assert decision.market_risk_state == "CAUTION"


def test_signal_carries_context_evidence_fields():
    sig = _signal(SignalAction.ENTER_LONG)
    sig.news_sentiment = -0.4
    sig.context_conflict_count = 1
    assert sig.mover_bias == "loser"
    assert sig.rs_score == 0.40
    assert sig.news_sentiment == -0.4


def test_thesis_continuity_blocks_repeated_same_context_losses():
    now = datetime.now(UTC)

    class DB:
        def get_shadow_signals(self, limit=500):
            return [
                {
                    "symbol": "TEST",
                    "direction": "long",
                    "timestamp": (now - timedelta(minutes=20)).isoformat(),
                    "regime": "range",
                    "mover_bias": "gainer",
                    "outcomes": {"60m": {"realized_r": -0.8}},
                },
                {
                    "symbol": "TEST",
                    "direction": "long",
                    "timestamp": (now - timedelta(minutes=8)).isoformat(),
                    "regime": "range",
                    "mover_bias": "gainer",
                    "outcomes": {"60m": {"realized_r": -0.6}},
                },
            ]

    sig = _signal(SignalAction.ENTER_LONG)
    sig.regime = Regime.RANGE
    sig.mover_bias = "gainer"
    result = assess_thesis_continuity(DB(), sig, now)

    assert result.allowed is False
    assert "thesis_continuity_conflict: repeated_same_context_losses" in result.reasons
    assert result.evidence["reentry_policy"] == "hard_block"
