from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from app.core.domain import Regime, SignalAction
from app.strategies.entry_intelligence import assess_entry_context, assess_thesis_continuity


def _sig(side: str, **overrides):
    values = {
        "symbol": "SMCI",
        "action": SignalAction.ENTER_LONG if side == "long" else SignalAction.ENTER_SHORT,
        "side": side,
        "confidence": 0.8,
        "strategy": "momentum",
        "reason": "regression",
        "regime": Regime.TREND_UP,
        "mover_bias": None,
        "rs_score": None,
        "news_context_available": False,
        "news_direction": "neutral",
        "news_confidence": 0.0,
        "news_catalyst": None,
        "news_age_seconds": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _df(closes, atr=1.0):
    closes = list(map(float, closes))
    return pd.DataFrame({
        "close": closes,
        "high": [x + 0.2 for x in closes],
        "low": [x - 0.2 for x in closes],
        "atr14": [atr] * len(closes),
        "ema9": [closes[-1]] * len(closes),
        "ema20": [closes[-1]] * len(closes),
    })


def test_short_into_confirmed_uptrend_is_rejected():
    sig = _sig("short", mover_bias="gainer", rs_score=0.62)
    result = assess_entry_context(sig, _df([100 + i * 0.25 for i in range(30)]), Regime.TREND_UP)
    assert result.allowed is False
    assert "entry_conviction_hard_reject" in result.reasons


def test_long_into_confirmed_downtrend_is_rejected():
    sig = _sig("long", mover_bias="loser", rs_score=0.38, regime=Regime.TREND_DOWN)
    result = assess_entry_context(sig, _df([110 - i * 0.25 for i in range(30)]), Regime.TREND_DOWN)
    assert result.allowed is False


def test_confirmed_reversal_remains_tradeable():
    sig = _sig("short", regime=Regime.TREND_UP)
    closes = [104, 103.8, 103.6, 103.4, 103.2, 103.0, 102.8, 102.6, 102.0, 101.4, 100.8, 100.0]
    result = assess_entry_context(sig, _df(closes), Regime.TREND_UP)
    assert result.allowed is True


def test_fresh_adverse_material_news_plus_price_confirmation_is_rejected():
    sig = _sig("long", news_context_available=True, news_direction="bearish", news_confidence=0.95, news_catalyst="financing", news_age_seconds=60)
    closes = [100 + i * 0.15 for i in range(20)] + [103.0, 102.7, 102.4, 102.1, 101.8]
    result = assess_entry_context(sig, _df(closes), Regime.TREND_UP)
    assert result.allowed is False
    assert any("news_context_conflict" in r for r in result.reasons)


def test_two_recent_same_context_losses_block_reentry():
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    class DB:
        def get_shadow_signals(self, limit=500):
            return [
                {"symbol": "SMCI", "direction": "long", "timestamp": (now - timedelta(minutes=20)).isoformat(), "regime": "trend_up", "mover_bias": "gainer", "outcomes": {"60m": {"realized_r": -0.8}}},
                {"symbol": "SMCI", "direction": "long", "timestamp": (now - timedelta(minutes=8)).isoformat(), "regime": "trend_up", "mover_bias": "gainer", "outcomes": {"60m": {"realized_r": -0.6}}},
            ]
    sig = _sig("long", mover_bias="gainer", rs_score=0.62)
    result = assess_thesis_continuity(DB(), sig, now)
    assert result.allowed is False
    assert "thesis_continuity_conflict: repeated_same_context_losses" in result.reasons


def test_fresh_supportive_catalyst_resets_reentry_penalty():
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    class DB:
        def get_shadow_signals(self, limit=500):
            return [
                {"symbol": "SMCI", "direction": "short", "timestamp": (now - timedelta(minutes=20)).isoformat(), "regime": "trend_up", "mover_bias": "gainer", "outcomes": {"60m": {"realized_r": -0.8}}},
                {"symbol": "SMCI", "direction": "short", "timestamp": (now - timedelta(minutes=8)).isoformat(), "regime": "trend_up", "mover_bias": "gainer", "outcomes": {"60m": {"realized_r": -0.6}}},
            ]
    sig = _sig("short", mover_bias="gainer", rs_score=0.62, news_context_available=True, news_direction="bearish", news_confidence=0.9, news_catalyst="earnings", news_age_seconds=30)
    result = assess_thesis_continuity(DB(), sig, now)
    assert result.allowed is True
    assert result.evidence["reentry_policy"] == "fresh_supportive_catalyst_resets_thesis"
