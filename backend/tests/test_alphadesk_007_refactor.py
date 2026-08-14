from __future__ import annotations

from app.core.alphadesk_007_refactor import PRODUCTION_STRATEGIES


def test_momentum_remains_production_strategy():
    assert "momentum" in PRODUCTION_STRATEGIES


def test_production_strategy_set_is_small_and_explicit():
    assert PRODUCTION_STRATEGIES == {
        "momentum",
        "orb",
        "rsi2_reversion",
        "vwap_reversion",
        "trend_pullback",
    }
