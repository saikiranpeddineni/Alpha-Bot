"""Entry chase veto tests -- vertical-candle chasing is rejected, normal bars pass."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


from types import SimpleNamespace


def _features(df):
    """Neutral but complete feature vector -- the chase veto reads the BAR data
    (high/low/close/atr), not the feature row; downstream stages are mocked."""
    import pandas as pd
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
    df["atr14"] = spread * 2.0  # normal bar range == atr -> tr_atr ~1x for calm bars
    return df


def _sig(action):
    return Signal(symbol="AAPL", action=action, confidence=0.8, strategy="t",
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


def test_vertical_candle_long_chase_rejected():
    # calm drift up then one huge vertical bar closing at its high
    closes = [100 + 0.02 * i for i in range(120)] + [103.0]
    df = _bars(closes)
    accepted, rejected = _pipeline().process("AAPL", df, [_sig(SignalAction.ENTER_LONG)], Regime.TREND_UP,
                                             features=_features(df))
    assert not accepted and len(rejected) == 1
    assert "extended signal bar" in rejected[0][1]


def test_vertical_candle_short_chase_rejected():
    closes = [100 - 0.02 * i for i in range(120)] + [97.0]
    df = _bars(closes)
    accepted, rejected = _pipeline().process("AAPL", df, [_sig(SignalAction.ENTER_SHORT)], Regime.TREND_DOWN,
                                             features=_features(df))
    assert not accepted
    assert "extended signal bar" in rejected[0][1]


def test_normal_bar_reaches_ml_stage_not_chase_vetoed():
    # steady small bars; last bar range ~= ATR -> ratio ~1x < 1.8
    closes = [100 + 0.02 * i for i in range(121)]
    df = _bars(closes)
    p = _pipeline()
    # force ML pass so we can see it survived the veto
    accepted, rejected = p.process("AAPL", df, [_sig(SignalAction.ENTER_LONG)], Regime.TREND_UP, features=None)
    reasons = " | ".join(r for _, r in rejected)
    assert "extended signal bar" not in reasons


def test_chase_veto_can_be_disabled():
    closes = [100 + 0.02 * i for i in range(120)] + [103.0]
    df = _bars(closes)
    p = _pipeline(enable_entry_chase_veto=False)
    accepted, rejected = p.process("AAPL", df, [_sig(SignalAction.ENTER_LONG)], Regime.TREND_UP, features=None)
    assert all("extended signal bar" not in r for _, r in rejected)
