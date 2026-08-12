"""Execute the market scenario catalog against the live entry-intelligence code."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.core.domain import SignalAction
from app.strategies.entry_intelligence import assess_entry_context
from .market_scenarios import SCENARIOS


def _signal(scenario):
    return SimpleNamespace(
        action=SignalAction.ENTER_LONG if scenario.side == "long" else SignalAction.ENTER_SHORT,
        side=scenario.side,
        strategy="momentum",
        symbol="SCENARIO",
        mover_bias=scenario.mover_bias,
        rs_score=scenario.rs_score,
        news_context_available=False,
        news_direction="neutral",
        news_sentiment=0.0,
        news_confidence=0.0,
        news_catalyst=None,
        news_age_seconds=None,
        regime=scenario.regime,
    )


def _bars(closes, mover_bias):
    # Adverse mover fixtures deliberately include an opposing EMA structure so
    # the test supplies the second independent contradiction required by the
    # production entry-intelligence contract.
    if mover_bias == "loser":
        ema9, ema20 = closes[-1] - 1.0, closes[-1] + 1.0
    elif mover_bias == "gainer":
        ema9, ema20 = closes[-1] + 1.0, closes[-1] - 1.0
    else:
        ema9 = ema20 = closes[-1]
    return pd.DataFrame({
        "close": list(closes),
        "high": [x + .20 for x in closes],
        "low": [x - .20 for x in closes],
        "atr14": [1.0] * len(closes),
        "ema9": [ema9] * len(closes),
        "ema20": [ema20] * len(closes),
        "volume": [100_000] * len(closes),
    })


def test_all_market_fixtures_execute_through_canonical_entry_intelligence():
    assert len(SCENARIOS) == 13
    for scenario in SCENARIOS:
        result = assess_entry_context(_signal(scenario), _bars(scenario.closes, scenario.mover_bias), scenario.regime)
        assert result.allowed is scenario.expect_allowed, (
            f"{scenario.name}: expected allowed={scenario.expect_allowed}, "
            f"got {result.allowed}; reasons={result.reasons}"
        )
