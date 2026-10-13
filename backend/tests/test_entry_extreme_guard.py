"""Session-extreme proximity guard tests -- entries INTO session extremes are
rejected for momentum strategies; mean-reversion and mid-range setups pass."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.domain import Regime, Signal, SignalAction
from app.strategies.pipeline import SignalPipeline


class _DummyModel:
    """Model that would PASS everything if reached."""

    def __init__(self):
        self._meta_label_engine = None

    def score_action_detailed(self, features, action, symbol=None, regime_key=None):
        return SimpleNamespace(probability=0.9, model_source="BASE",
                               specialist_status="NO_ARTIFACT", specialist_auc=None,
                               specialist_min_auc=0.53, fallback_reason=None)

    def score_action(self, features, action, symbol=None, regime_key=None):
        return 0.9


def _features(df):
    from app.ai.features import FEATURE_COLUMNS
    return pd.Series(0.0, index=FEATURE_COLUMNS)


def _bars(close_path, spread=0.10):
    """Build an OHLCV frame from a close path with fixed per-bar range."""
    rows = []
    ts = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
    prev = close_path[0]
    for i, c in enumerate(close_path[1:], start=1):
        hi = max(prev, c) + spread / 2
        lo = min(prev, c) - spread / 2
        rows.append((ts + timedelta(minutes=i), prev, hi, lo, c, 1000.0))
        prev = c
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    df.index = pd.DatetimeIndex(df.index)
    df["atr14"] = spread * 2.0
    return df


def _sig(action, strategy="t"):
    return Signal(symbol="AAPL", action=action, confidence=0.8, strategy=strategy,
                  reason="r", price=None, stop_loss=None, take_profit=None)


def _pipeline(**cfg_over):
    rc = RiskConfig(ml_confidence_threshold=0.5, **cfg_over)
    p = SignalPipeline.__new__(SignalPipeline)
    from app.ai.prediction import TradeScoreEngine
    p.model = _DummyModel()
    p.ml_threshold = 0.5
    p.soft_composite_gating = False
    p.regime_weights = {}
    settings_like = SimpleNamespace(trade_score_weights=lambda: (0.3, 0.25, 0.2, 0.1, 0.1, 0.05))
    p.trade_score_engine = TradeScoreEngine(weights=settings_like.trade_score_weights(), risk_config=rc)
    p.meta_label_engine = MagicMock()
    meta_res = SimpleNamespace(approved=True, decision="PASS", source="test",
                               reason="", prob_profitable=0.9)
    p.meta_label_engine.predict.return_value = meta_res
    p._is_event_day = False
    return p


def test_short_at_session_low_rejected():
    # sustained decline; decision bar sits at the low-of-session
    closes = [100 - 0.05 * i for i in range(120)]
    df = _bars(closes)
    accepted, rejected = _pipeline().process(
        "AAPL", df, [_sig(SignalAction.ENTER_SHORT)], Regime.TREND_DOWN,
        features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" in reasons


def test_long_at_session_high_rejected():
    closes = [100 + 0.05 * i for i in range(120)]
    df = _bars(closes)
    accepted, rejected = _pipeline().process(
        "AAPL", df, [_sig(SignalAction.ENTER_LONG)], Regime.TREND_UP,
        features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" in reasons


def test_short_mid_range_not_vetoed():
    # decline to a low then recovery; entry far above the session low and
    # (for a short) far below the high -> guard must stay silent.
    closes = [100 - 0.10 * i for i in range(60)] + [90 + 0.20 * i for i in range(1, 61)]
    df = _bars(closes)
    accepted, rejected = _pipeline().process(
        "AAPL", df, [_sig(SignalAction.ENTER_SHORT)], Regime.RANGE,
        features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" not in reasons


def test_guard_can_be_disabled():
    closes = [100 - 0.05 * i for i in range(120)]
    df = _bars(closes)
    accepted, rejected = _pipeline(enable_entry_extreme_guard=False).process(
        "AAPL", df, [_sig(SignalAction.ENTER_SHORT)], Regime.TREND_DOWN,
        features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" not in reasons


def test_mean_reversion_exempt_from_guard():
    # vwap_reversion is is_mean_reversion=True: fading TOWARD an extreme is
    # its thesis, so a short near the session low must NOT be guard-blocked.
    closes = [100 - 0.05 * i for i in range(120)]
    df = _bars(closes)
    accepted, rejected = _pipeline().process(
        "AAPL", df, [_sig(SignalAction.ENTER_SHORT, strategy="vwap_reversion")],
        Regime.TREND_DOWN, features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" not in reasons


def test_location_veto_independent_of_chase_veto_flag():
    # regression: the location veto was historically nested inside the chase
    # veto's condition; disabling the chase veto must not disable it.
    closes = [100 + 0.02 * i for i in range(121)]
    df = _bars(closes)
    vwap = float(df["close"].iloc[-1]) + 10 * float(df["atr14"].iloc[-1])
    df["vwap"] = vwap  # price ~10 ATR BELOW vwap -> short entry is exhausted-location
    accepted, rejected = _pipeline(enable_entry_chase_veto=False).process(
        "AAPL", df, [_sig(SignalAction.ENTER_SHORT)], Regime.TREND_UP,
        features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "location exhausted" in reasons


@pytest.mark.parametrize("strat", ["orb", "bb_squeeze"])
def test_breakout_strategy_exempt_from_guard(strat):
    # Structural breakout strategies enter at or through session extremes by definition.
    # Long at the session high must NOT be guard-blocked.
    closes = [100 + 0.05 * i for i in range(120)]
    df = _bars(closes)
    accepted, rejected = _pipeline().process(
        "AAPL", df, [_sig(SignalAction.ENTER_LONG, strategy=strat)],
        Regime.TREND_UP, features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" not in reasons


@pytest.mark.parametrize("strat", ["momentum", "gap_and_go"])
def test_continuation_strategy_is_not_exempt_from_guard(strat):
    closes = [100 + 0.05 * i for i in range(120)]
    df = _bars(closes)
    _, rejected = _pipeline().process(
        "AAPL", df, [_sig(SignalAction.ENTER_LONG, strategy=strat)],
        Regime.TREND_UP, features=_features(df))
    reasons = " | ".join(r for _, r in rejected)
    assert "chasing session extreme" in reasons

