"""Tests for watchlist resilience, broker asset-level shortability separation,
and PIS noise-floor breathing room invariants."""

from datetime import UTC, datetime, timezone
from unittest.mock import MagicMock
import pytest

from app.brokers.alpaca import AlpacaBroker
from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType
from app.pis.models import PISConfig, PositionObservation, ExitAction
from app.pis.position_brain import PositionBrain
from app.pis.thesis_drift import ThesisDriftEvaluator
from app.pis.engine_adapter import PISEngineAdapter


def test_asset_level_unshortable_does_not_disable_account_shorting(monkeypatch):
    """An unshortable ticker (e.g. ONDS) must not disable shorting globally for the account."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = MagicMock()
    broker._shorting_enabled = True
    broker._shortable_cache = {}
    broker._symbol_strategies = {}
    broker._meta = {}

    exc = Exception('Order rejected ONDS sell 100: {"code":40310000,"message":"asset is not shortable"}')
    monkeypatch.setattr(broker, "_is_definitive_rejection", lambda e: True)
    broker._client.submit_order = MagicMock(side_effect=exc)

    req = OrderRequest(
        symbol="ONDS",
        side=OrderSide.SELL,
        qty=100,
        order_type=OrderType.MARKET,
        strategy="momentum",
        reason="test_short",
    )

    import asyncio
    order = asyncio.run(broker.submit(req))
    assert order.status == OrderStatus.REJECTED
    # Asset cache must be False for ONDS
    assert broker._shortable_cache.get("ONDS") is False
    # Account shorting MUST remain True!
    assert broker._shorting_enabled is True


def test_pis_engine_adapter_enforces_minimum_risk_floor():
    """Verify that risk_per_share cannot collapse below 0.5% of price on high-dollar stocks."""
    import pandas as pd
    from types import SimpleNamespace

    adapter = PISEngineAdapter(config=PISConfig())
    # Mock high-dollar position (AVGO @ $362.99)
    pos = SimpleNamespace(
        symbol="AVGO",
        qty=48.0,
        avg_entry_price=362.99,
        current_price=362.73,
        market_price=362.73,
        opened_at=datetime.now(UTC),
    )
    # 1-minute bar with tiny ATR of 0.20
    df = pd.DataFrame([{
        "open": 362.90,
        "high": 363.00,
        "low": 362.70,
        "close": 362.73,
        "volume": 1000.0,
        "atr14": 0.20,
    }])

    import asyncio
    pis_order, pis_decision = asyncio.run(adapter.evaluate_position_exit(
        position=pos,
        bar_df=df,
        initial_stop=None,  # Missing initial stop
    ))

    # Min risk floor for AVGO ($362.99) is 362.99 * 0.005 = $1.815
    # The 26c drop (-0.26 / 1.815) is only -0.14R, NOT -0.55R!
    # Therefore, position must NOT be exited on this 26c noise tick.
    assert pis_decision.action != ExitAction.FULL_EXIT
    assert pis_decision.thesis.early_invalidation_detected is False


def test_early_invalidation_requires_two_bars():
    """Early thesis invalidation requires at least 2 bars of evidence; bar 1 must never trigger it."""
    evaluator = ThesisDriftEvaluator(PISConfig(early_invalidation_min_bars=2))
    obs = PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol="TEST",
        side=1.0,
        qty=100,
        entry_price=100.0,
        current_price=98.5,
        high=100.0,
        low=98.5,
        close=98.5,
        volume=5000.0,
        atr=1.0,
        initial_stop=98.0,
        risk_per_share=2.0,
        r_multiple=-0.75,
    )

    # Bar 1 (instant fill bar): must NOT trigger early invalidation
    drift_bar1 = evaluator.evaluate(obs, trough_r=-0.75, stagnation_bars=0, peak_r=0.0, history_len=1)
    assert drift_bar1.early_invalidation_detected is False

    # Bar 2 (confirmed bar): triggers early invalidation
    drift_bar2 = evaluator.evaluate(obs, trough_r=-0.75, stagnation_bars=0, peak_r=0.0, history_len=2)
    assert drift_bar2.early_invalidation_detected is True
