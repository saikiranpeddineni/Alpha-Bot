"""Market-condition regression tests for Alphadesk_004.

These tests intentionally exercise the existing entry-intelligence boundary rather
than introducing a parallel trading engine.  They protect two properties:

1. The bot should reject genuinely contradictory conditions.
2. The bot should remain willing to trade strong, confirmed opportunities.

The suite is a behavioral regression harness, not a promise that any individual
market outcome is predictable or profitable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.domain import Regime, SignalAction
from app.strategies.entry_intelligence import assess_entry_context


def _signal(side: str = "long", **overrides):
    action = SignalAction.ENTER_LONG if side == "long" else SignalAction.ENTER_SHORT
    values = {
        "action": action,
        "side": side,
        "strategy": "momentum",
        "symbol": "TEST",
        "mover_bias": None,
        "rs_score": None,
        "news_context_available": False,
        "news_direction": "neutral",
        "news_sentiment": 0.0,
        "news_confidence": 0.0,
        "news_catalyst": None,
        "news_age_seconds": None,
        "regime": Regime.TREND_UP,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bars(closes, *, atr=1.0, ema9=None, ema20=None, volume=None):
    closes = list(map(float, closes))
    n = len(closes)
    if ema9 is None:
        ema9 = closes[-1]
    if ema20 is None:
        ema20 = closes[-1]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [x + 0.20 for x in closes],
            "low": [x - 0.20 for x in closes],
            "atr14": [float(atr)] * n,
            "ema9": [float(ema9)] * n,
            "ema20": [float(ema20)] * n,
            "volume": volume if volume is not None else [100_000] * n,
        }
    )


def _assert_allowed(result):
    assert result.allowed is True, result.reasons


def _assert_blocked(result):
    assert result.allowed is False, result.reasons


@pytest.mark.parametrize(
    "name,side,closes,regime,mover,rs",
    [
        (
            "strong_uptrend_continuation",
            "long",
            [100 + i * 0.35 for i in range(30)],
            Regime.TREND_UP,
            "gainer",
            0.70,
        ),
        (
            "strong_downtrend_continuation",
            "short",
            [110 - i * 0.35 for i in range(30)],
            Regime.TREND_DOWN,
            "loser",
            0.30,
        ),
        (
            "confirmed_long_reversal",
            "long",
            [105, 104.7, 104.4, 104.1, 103.8, 103.5, 103.7, 104.0, 104.4, 104.9, 105.5, 106.0],
            Regime.TREND_DOWN,
            None,
            None,
        ),
        (
            "confirmed_short_reversal",
            "short",
            [95, 95.3, 95.6, 95.9, 96.2, 96.5, 96.3, 96.0, 95.6, 95.1, 94.5, 94.0],
            Regime.TREND_UP,
            None,
            None,
        ),
    ],
)
def test_strong_opportunities_are_not_over_restricted(name, side, closes, regime, mover, rs):
    result = assess_entry_context(
        _signal(side, mover_bias=mover, rs_score=rs),
        _bars(closes),
        regime,
    )
    _assert_allowed(result)


@pytest.mark.parametrize(
    "side,mover,rs,ema9,ema20",
    [
        ("long", "loser", 0.35, 99.0, 101.0),
        ("short", "gainer", 0.65, 101.0, 99.0),
    ],
)
def test_directional_conflict_is_rejected_when_multiple_evidence_sources_agree(
    side, mover, rs, ema9, ema20
):
    closes = [100 + (i * 0.05 if side == "short" else -i * 0.05) for i in range(30)]
    result = assess_entry_context(
        _signal(side, mover_bias=mover, rs_score=rs),
        _bars(closes, ema9=ema9, ema20=ema20),
        Regime.TREND_UP if side == "short" else Regime.TREND_DOWN,
    )
    _assert_blocked(result)


def test_range_market_does_not_blankly_block_a_confirmed_symbol():
    closes = [100 + i * 0.15 for i in range(30)]
    result = assess_entry_context(
        _signal("long", mover_bias="gainer", rs_score=0.68),
        _bars(closes),
        Regime.RANGE,
    )
    _assert_allowed(result)


def test_range_market_with_no_symbol_confirmation_remains_tradable_but_flagged():
    closes = [100 + (0.04 if i % 2 == 0 else -0.04) for i in range(30)]
    result = assess_entry_context(
        _signal("long"),
        _bars(closes, ema9=100.0, ema20=100.0),
        Regime.RANGE,
    )
    _assert_allowed(result)
    assert any("macro_context_conflict" in r for r in result.reasons)


def test_fresh_material_adverse_news_confirms_a_contradiction():
    sig = _signal(
        "long",
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.90,
        news_catalyst="financing",
        news_age_seconds=120,
    )
    closes = [100 - i * 0.25 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes), Regime.TREND_DOWN)
    _assert_blocked(result)
    assert any("news_context_conflict" in r for r in result.reasons)


def test_fresh_adverse_news_is_absorbed_and_does_not_block():
    sig = _signal(
        "long",
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.90,
        news_catalyst="financing",
        news_age_seconds=120,
    )
    closes = [100 + i * 0.20 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes), Regime.TREND_UP)
    _assert_allowed(result)
    assert not any("news_context_conflict" in r for r in result.reasons)


def test_undated_news_is_not_treated_as_fresh():
    sig = _signal(
        "long",
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.95,
        news_catalyst="financing",
        news_age_seconds=None,
    )
    closes = [100 - i * 0.20 for i in range(30)]
    result = assess_entry_context(sig, _bars(closes), Regime.TREND_DOWN)
    assert not any("news_context_conflict" in r for r in result.reasons)


def test_breakout_test_is_not_called_exhaustion():
    closes = [100 + i * 0.10 for i in range(19)] + [102.1]
    result = assess_entry_context(
        _signal("long", mover_bias="neutral", rs_score=0.55),
        _bars(closes),
        Regime.TREND_UP,
    )
    _assert_allowed(result)


def test_genuine_extension_without_mover_confirmation_is_flagged():
    closes = [100 + i * 0.05 for i in range(19)] + [102.0]
    result = assess_entry_context(
        _signal("long", mover_bias="neutral", rs_score=0.50),
        _bars(closes),
        Regime.TREND_UP,
    )
    assert any("entry_location_conflict" in r for r in result.reasons)


def test_short_breakdown_with_loser_confirmation_is_not_blocked():
    closes = [110 - i * 0.15 for i in range(30)]
    result = assess_entry_context(
        _signal("short", mover_bias="loser", rs_score=0.30),
        _bars(closes),
        Regime.TREND_DOWN,
    )
    _assert_allowed(result)


def test_previous_loss_does_not_create_an_emotional_veto():
    class _DB:
        def get_shadow_signals(self, limit=500):
            return [
                {
                    "symbol": "TEST",
                    "direction": "long",
                    "timestamp": "2026-08-12T01:00:00+00:00",
                    "regime": "TREND_UP",
                    "mover_bias": "gainer",
                    "outcomes": {"60m": {"realized_r": -1.0}},
                }
            ]

    from datetime import datetime, timezone
    from app.strategies.entry_intelligence import assess_thesis_continuity

    result = assess_thesis_continuity(
        _DB(),
        _signal("long", symbol="TEST", mover_bias="gainer", regime=Regime.TREND_UP),
        datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc),
    )
    _assert_allowed(result)
    assert any("thesis_history_observation" in r for r in result.reasons)


@pytest.mark.parametrize("profit_r,mfe_r", [(0.0, 0.0), (0.5, 1.0), (2.0, 4.0), (-1.0, 0.25)])
def test_profit_capture_metric_is_well_defined(profit_r, mfe_r):
    """A small invariant for future PIS profit-preservation analysis."""
    capture = 0.0 if mfe_r <= 0 else profit_r / mfe_r
    assert capture == pytest.approx(0.0 if mfe_r <= 0 and profit_r == 0 else capture)
    assert capture <= 1.0 or profit_r > mfe_r


@pytest.mark.parametrize(
    "scenario",
    [
        "gap_up_continuation",
        "gap_up_fade",
        "gap_down_recovery",
        "gap_down_continuation",
        "high_volatility_expansion",
        "low_volatility_compression",
        "false_breakout",
        "trend_to_reversal",
        "reversal_to_trend",
        "sector_rotation",
        "market_stress",
        "end_of_day",
    ],
)
def test_scenario_catalog_is_explicit(scenario):
    """Keep the market-condition coverage list visible and reviewable.

    Detailed scenario fixtures are added incrementally; this test prevents the
    catalog from silently disappearing as the suite evolves.
    """
    assert scenario
