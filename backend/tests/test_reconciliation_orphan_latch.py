"""Tests for R1 Bi-Directional Orphan Auto-Adopt & Drain Reconciliation Latch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position
from app.core.engine import TradingEngine
from app.portfolio.tracker import PortfolioTracker, _OpenLot


class MockBroker:
    def __init__(self, positions=None, open_orders=None):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.closed_positions = []

    async def positions(self):
        return self._positions

    async def open_orders(self):
        return self._open_orders

    async def close_position(self, symbol, reason="", qty=None, strategy=None):
        self.closed_positions.append((symbol, reason, qty))
        return Order(
            id=f"close_{symbol}",
            client_order_id=f"close_{symbol}",
            symbol=symbol,
            side=OrderSide.SELL,
            qty=qty or 10.0,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            reason=reason,
        )


class MockDatabase:
    def get_latest_entry_order_payload(self, symbol, mode):
        return None

    def get_latest_order_payload(self, symbol, mode):
        return None

    def get_latest_entry_order_strategy(self, symbol, mode):
        return "orb"

    def save_order(self, order, mode):
        pass


@pytest.fixture
def base_engine():
    engine = object.__new__(TradingEngine)
    engine.mode = "paper"
    engine.db = MockDatabase()
    engine.portfolio = PortfolioTracker()
    engine._position_reconciliation_mismatch_streak = 0
    engine.reconciliation_mismatch = False
    engine.reconciliation_failed = False
    engine.orphan_reconciliation_events = []
    engine._position_take_profits = {}
    engine._position_peaks = {}
    engine._position_troughs = {}
    engine._position_initial_stops = {}
    engine.recent_orders = SimpleNamespace(appendleft=MagicMock())
    engine._flattening_symbols = set()
    engine._pending_close_symbols = set()
    engine._pending_close_order_ids = {}
    engine._pending_close_qtys = {}
    engine._pending_close_submitted_at = {}
    engine.publish = MagicMock()
    return engine


@pytest.mark.asyncio
async def test_transient_in_flight_mismatch_does_not_increment_streak(base_engine):
    """Mismatches accounted for by in-flight open orders do not advance streak counter."""
    broker_pos = [Position(symbol="AAPL", qty=10.0, avg_entry_price=150.0, market_price=150.0)]
    open_orders = [SimpleNamespace(symbol="AAPL")]
    base_engine.broker = MockBroker(positions=broker_pos, open_orders=open_orders)
    # DB has 0 lots for AAPL
    diagnostics = {}
    now = datetime.now(UTC)

    bad = await base_engine._evaluate_position_reconciliation(
        broker_positions=broker_pos,
        lots=base_engine.portfolio._lots,
        open_orders=open_orders,
        diagnostics=diagnostics,
        now=now,
    )
    assert bad is True
    assert base_engine._position_reconciliation_mismatch_streak == 0


@pytest.mark.asyncio
async def test_orphan_drain_ghost_local_lot(base_engine):
    """Ghost DB lot with broker confirmed 0 position triggers WARN_ORPHAN_DRAINED after 3 streaks."""
    # DB has ghost lot for TSLA
    base_engine.portfolio._lots["TSLA"] = _OpenLot(
        qty=50.0,
        avg_price=200.0,
        entry_ts=datetime.now(UTC),
        strategy="orb",
        stop_loss=195.0,
        initial_risk_per_share=5.0,
    )
    base_engine.broker = MockBroker(positions=[], open_orders=[])
    diagnostics = {}
    now = datetime.now(UTC)

    # Tick 1: streak -> 1
    await base_engine._evaluate_position_reconciliation(
        broker_positions=[],
        lots=base_engine.portfolio._lots,
        open_orders=[],
        diagnostics=diagnostics,
        now=now,
    )
    assert base_engine._position_reconciliation_mismatch_streak == 1
    assert "TSLA" in base_engine.portfolio._lots

    # Tick 2: streak -> 2
    await base_engine._evaluate_position_reconciliation(
        broker_positions=[],
        lots=base_engine.portfolio._lots,
        open_orders=[],
        diagnostics=diagnostics,
        now=now,
    )
    assert base_engine._position_reconciliation_mismatch_streak == 2
    assert "TSLA" in base_engine.portfolio._lots

    # Tick 3: streak -> 3 (triggers auto-reconciliation latch)
    with patch("app.core.reconciliation.applog.warning") as mock_warn:
        await base_engine._evaluate_position_reconciliation(
            broker_positions=[],
            lots=base_engine.portfolio._lots,
            open_orders=[],
            diagnostics=diagnostics,
            now=now,
        )
        assert base_engine._position_reconciliation_mismatch_streak >= 3
        # TSLA ghost lot was drained
        assert "TSLA" not in base_engine.portfolio._lots
        # Audit log WARN_ORPHAN_DRAINED was emitted
        warn_calls = [call.args[1] for call in mock_warn.call_args_list if len(call.args) > 1]
        assert any("WARN_ORPHAN_DRAINED" in msg for msg in warn_calls)
        # Event recorded in orphan_reconciliation_events
        assert any(e["event"] == "WARN_ORPHAN_DRAINED" and e["symbol"] == "TSLA" for e in base_engine.orphan_reconciliation_events)


@pytest.mark.asyncio
async def test_orphan_adopt_broker_held_position(base_engine):
    """Broker holds orphan position absent in local DB: triggers WARN_ORPHAN_ADOPTED with protective stop."""
    broker_pos = [Position(symbol="NVDA", qty=25.0, avg_entry_price=120.0, market_price=120.0)]
    base_engine.broker = MockBroker(positions=broker_pos, open_orders=[])
    diagnostics = {}
    now = datetime.now(UTC)

    # Advance streak to 2
    base_engine._position_reconciliation_mismatch_streak = 2

    # 3rd mismatch triggers latch
    with patch("app.core.reconciliation.applog.warning") as mock_warn:
        await base_engine._evaluate_position_reconciliation(
            broker_positions=broker_pos,
            lots=base_engine.portfolio._lots,
            open_orders=[],
            diagnostics=diagnostics,
            now=now,
        )
        assert base_engine._position_reconciliation_mismatch_streak >= 3
        # Check audit log calls for WARN_ORPHAN_ADOPTED
        warn_calls = [call.args[1] for call in mock_warn.call_args_list if len(call.args) > 1]
        assert any("WARN_ORPHAN_ADOPTED" in msg for msg in warn_calls)
        # Check event recorded
        adopted_events = [e for e in base_engine.orphan_reconciliation_events if e["event"] == "WARN_ORPHAN_ADOPTED"]
        assert len(adopted_events) >= 1
        assert adopted_events[0]["symbol"] == "NVDA"
        assert adopted_events[0]["qty"] == 25.0
        # Check protective stop was assigned
        assert adopted_events[0]["stop_loss"] is not None
