from __future__ import annotations

from app.core.alphadesk_007_refactor import PRODUCTION_STRATEGIES
from app.strategies.base import create_strategies


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


def test_production_factory_enables_momentum_and_disables_research():
    strategies = {s.name: s for s in create_strategies()}
    assert strategies["momentum"].enabled is True
    assert strategies["orb"].enabled is True
    assert strategies["rsi2_reversion"].enabled is True
    assert strategies["vwap_reversion"].enabled is True
    assert strategies["trend_pullback"].enabled is True
    for name, strategy in strategies.items():
        if name not in PRODUCTION_STRATEGIES:
            assert strategy.enabled is False
