from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.entry_intelligence import assess_catalyst_conviction, assess_entry_context, assess_thesis_continuity
from app.strategies.pipeline import SignalPipeline


def _sig(side: str, **overrides):
    values = {
        "symbol": "SMCI",
        "action": SignalAction.ENTER_LONG if side == "long" else SignalAction.ENTER_SHORT,
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
        "news_sentiment": 0.0,
        "price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 105.0,
        "candidate_id": "REGRESSION",
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
        "volume": [100_000] * len(closes),
        "vol_sma20": [100_000] * len(closes),
    }, index=pd.date_range("2026-08-12 13:30", periods=len(closes), freq="min", tz="UTC"))


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


def test_exceptional_catalyst_rescues_existing_pipeline_without_threshold_change():
    class Model:
        def score_action(self, features, action, symbol=None, regime_key=None):
            return 0.30

    class Meta:
        def predict(self, *args, **kwargs):
            return SimpleNamespace(
                approved=False,
                prob_profitable=0.40,
                threshold=0.55,
                decision="REJECT",
                source="REAL_MODEL",
                reason="probability below threshold",
            )

    class Scores:
        def __init__(self):
            self.risk_config = SimpleNamespace(ml_hard_reject_threshold=0.48, min_stop_pct=0.5, midday_score_boost=0.0)

        def compute(self, **kwargs):
            return 40.0

        def approve(self, score, regime, is_event_day=False, threshold_boost=0.0):
            return score >= 60.0, 60.0

    pipeline = object.__new__(SignalPipeline)
    pipeline.model = Model()
    pipeline.ml_threshold = 0.55
    pipeline.soft_composite_gating = False
    pipeline._is_event_day = False
    pipeline._midday_active = False
    pipeline.trade_score_engine = Scores()
    pipeline.meta_label_engine = Meta()

    sig = _sig(
        "long",
        mover_bias="gainer",
        rs_score=0.70,
        news_context_available=True,
        news_direction="bullish",
        news_confidence=1.0,
        news_catalyst="earnings",
        news_age_seconds=60,
    )
    bars = _df([100 + i * 1.0 for i in range(30)])
    accepted, rejected = pipeline.process(
        "SMCI",
        bars,
        [sig],
        Regime.TREND_UP,
        features=pd.Series(dtype=float),
    )
    assert accepted == [sig]
    assert rejected == []
    assert sig.ml_probability == 0.55
    assert sig.ml_gate_decision == "CATALYST_CONTEXT_RESCUED"
