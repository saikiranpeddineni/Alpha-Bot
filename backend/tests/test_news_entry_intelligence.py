from datetime import UTC, datetime, timedelta

import pandas as pd

from app.core.domain import Regime, Signal, SignalAction
from app.pis.sentiment import NewsSentimentAnalyzer
from app.strategies.entry_intelligence import assess_entry_context


def _frame(long_trend: bool = False, breakout: bool = False) -> pd.DataFrame:
    rows = 25
    close = [100.0 + i * (0.2 if long_trend else -0.2) for i in range(rows)]
    if breakout:
        close[-1] = max(close[:-1]) + 0.25
    high = [v + 0.5 for v in close]
    low = [v - 0.5 for v in close]
    return pd.DataFrame(
        {
            "close": close,
            "high": high,
            "low": low,
            "atr14": [1.0] * rows,
            "ema9": [100.0] * rows,
            "ema20": [101.0] * rows if not long_trend else [99.0] * rows,
        }
    )


def _signal(action: SignalAction, **news) -> Signal:
    return Signal(
        symbol="INTC",
        action=action,
        confidence=0.8,
        strategy="momentum",
        reason="test",
        regime=Regime.TREND_UP,
        **news,
    )


def test_news_analyzer_identifies_financing_catalyst_and_direction():
    analyzer = NewsSentimentAnalyzer()
    now = datetime.now(UTC)
    context = analyzer.build_context(
        [{"headline": "INTC announces secondary share offering and dilution", "created_at": (now - timedelta(minutes=2)).isoformat()}],
        now=now,
    )
    assert context.direction == "bearish"
    assert context.catalyst == "financing"
    assert context.fresh_headline_count == 1
    assert context.headline_count == 1
    assert context.confidence > 0.5


def test_news_analyzer_understands_event_phrases_and_negation():
    analyzer = NewsSentimentAnalyzer()
    assert analyzer.analyze_headline("Company beats estimates and raises guidance") > 0.5
    assert analyzer.analyze_headline("Company misses estimates and cuts guidance") < -0.5
    assert analyzer.analyze_headline("Company denies dilution reports") > 0
    assert analyzer.catalyst_for_headline("Company faces SEC investigation after secondary offering") == "financing"


def test_news_analyzer_prefers_consensus_direction_and_freshness():
    analyzer = NewsSentimentAnalyzer()
    now = datetime.now(UTC)
    context = analyzer.build_context(
        [
            {"headline": "Company beats estimates and raises guidance", "created_at": (now - timedelta(minutes=2)).isoformat()},
            {"headline": "Analyst reiterates neutral rating", "created_at": (now - timedelta(hours=5)).isoformat()},
            {"headline": "Company wins major contract", "created_at": (now - timedelta(minutes=8)).isoformat()},
        ],
        now=now,
    )
    assert context.direction == "bullish"
    assert context.fresh_headline_count == 2
    assert context.confidence >= 0.5


def test_contradictory_headlines_do_not_create_false_high_confidence():
    analyzer = NewsSentimentAnalyzer()
    now = datetime.now(UTC)
    context = analyzer.build_context(
        [
            {"headline": "Company beats estimates and raises guidance", "created_at": (now - timedelta(minutes=2)).isoformat()},
            {"headline": "Company misses estimates and cuts guidance", "created_at": (now - timedelta(minutes=3)).isoformat()},
        ],
        now=now,
    )
    assert abs(context.sentiment) < 0.10
    assert context.confidence < 0.5


def test_undated_news_is_context_only_not_fresh():
    analyzer = NewsSentimentAnalyzer()
    now = datetime.now(UTC)
    context = analyzer.build_context([{"headline": "Company announces financing and dilution"}], now=now)
    assert context.headline_count == 1
    assert context.fresh_headline_count == 0
    assert context.newest_age_seconds is None
    assert context.confidence < 0.5


def test_bearish_financing_news_is_entry_contradiction_for_long():
    sig = _signal(
        SignalAction.ENTER_LONG,
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.9,
        news_catalyst="financing",
        news_age_seconds=120,
    )
    result = assess_entry_context(sig, _frame(long_trend=False), Regime.TREND_UP)
    assert result.allowed is False
    assert any("news_context_conflict" in reason for reason in result.reasons)


def test_bullish_news_does_not_block_long_on_its_own():
    sig = _signal(
        SignalAction.ENTER_LONG,
        news_context_available=True,
        news_direction="bullish",
        news_confidence=0.9,
        news_catalyst="corporate",
        news_age_seconds=120,
    )
    result = assess_entry_context(sig, _frame(long_trend=True), Regime.TREND_UP)
    assert result.allowed is True


def test_bullish_news_is_entry_contradiction_for_short_with_trend_conflict():
    sig = _signal(
        SignalAction.ENTER_SHORT,
        news_context_available=True,
        news_direction="bullish",
        news_confidence=0.9,
        news_catalyst="corporate",
        news_age_seconds=120,
    )
    result = assess_entry_context(sig, _frame(long_trend=True), Regime.TREND_UP)
    assert result.allowed is False
    assert any("news_context_conflict" in reason for reason in result.reasons)


def test_breakout_touch_is_not_extension_rejection():
    sig = _signal(SignalAction.ENTER_LONG, mover_bias=None, rs_score=None)
    result = assess_entry_context(sig, _frame(long_trend=True, breakout=False), Regime.TREND_UP)
    assert not any("entry_location_conflict" in reason for reason in result.reasons)
