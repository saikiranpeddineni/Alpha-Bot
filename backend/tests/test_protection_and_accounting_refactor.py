import pytest
from datetime import datetime, UTC
from unittest.mock import AsyncMock

from app.brokers.base import Fill
from app.core.domain import Order, OrderRequest, OrderSide, OrderType, OrderStatus, Position
from app.core.engine import TradingEngine
from app.portfolio.tracker import PortfolioTracker, _OpenLot


@pytest.mark.asyncio
async def test_missing_protection_submits_real_stop():
    engine = TradingEngine.__new__(TradingEngine)
    engine.portfolio = PortfolioTracker()
    engine.broker = AsyncMock()
    engine.broker.open_orders = AsyncMock(return_value=[])
    engine.broker.get_order_by_client_id = AsyncMock(return_value=None)
    engine.broker.submit = AsyncMock(return_value=Order(
        id="recovery_1", client_order_id="protect-AAPL", symbol="AAPL",
        side=OrderSide.SELL, qty=100.0, order_type=OrderType.STOP,
        stop_price=197.0, status=OrderStatus.SUBMITTED
    ))
    engine.allow_new_entries = True
    engine.system_health = "NORMAL"

    position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    engine.portfolio._lots["AAPL"] = _OpenLot(
        qty=100.0, avg_price=200.0, entry_ts=datetime.now(UTC), strategy="orb", initial_stop_loss=197.0
    )

    result = await engine.verify_broker_protective_stops([position])
    assert result["AAPL"] is False
    assert engine.allow_new_entries is False

    req = engine.broker.submit.call_args[0][0]
    assert req.symbol == "AAPL"
    assert req.side == OrderSide.SELL
    assert req.qty == 100.0
    assert req.order_type == OrderType.STOP
    assert req.stop_price == 197.0
    assert req.limit_price is None


@pytest.mark.asyncio
async def test_sell_limit_below_market_is_not_protection():
    engine = TradingEngine.__new__(TradingEngine)
    position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    fake_limit = Order(
        id="dangerous-limit", symbol="AAPL", side=OrderSide.SELL, qty=100.0,
        filled_qty=0.0, order_type=OrderType.LIMIT, limit_price=197.0, status=OrderStatus.SUBMITTED
    )

    assert engine._is_valid_protective_stop(fake_limit, position) is False


@pytest.mark.asyncio
async def test_rejected_recovery_never_restores_protection():
    engine = TradingEngine.__new__(TradingEngine)
    engine.portfolio = PortfolioTracker()
    engine.broker = AsyncMock()
    engine.broker.open_orders = AsyncMock(return_value=[])
    engine.broker.get_order_by_client_id = AsyncMock(return_value=None)
    rejected = Order(
        id="rejected", symbol="AAPL", side=OrderSide.SELL, qty=100.0,
        order_type=OrderType.STOP, stop_price=197.0, status=OrderStatus.REJECTED, reason="broker rejected"
    )
    engine.broker.submit = AsyncMock(return_value=rejected)
    engine.allow_new_entries = True
    engine.system_health = "NORMAL"

    position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    result = await engine.verify_broker_protective_stops([position])

    assert result["AAPL"] is False
    assert engine.allow_new_entries is False


@pytest.mark.asyncio
async def test_submit_ack_does_not_mean_protected():
    engine = TradingEngine.__new__(TradingEngine)
    engine.portfolio = PortfolioTracker()
    engine.broker = AsyncMock()
    engine.allow_new_entries = True
    engine.system_health = "NORMAL"

    position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    submitted = Order(
        id="stop-123", symbol="AAPL", side=OrderSide.SELL, qty=100.0,
        order_type=OrderType.STOP, stop_price=197.0, status=OrderStatus.SUBMITTED
    )

    # Cycle 1: open_orders is empty, recovery order submitted
    engine.broker.open_orders = AsyncMock(return_value=[])
    engine.broker.get_order_by_client_id = AsyncMock(return_value=None)
    engine.broker.submit = AsyncMock(return_value=submitted)
    engine.allow_new_entries = True

    status_c1 = await engine.verify_broker_protective_stops([position])
    assert status_c1["AAPL"] is False
    assert engine.allow_new_entries is False

    # Cycle 2: open_orders contains submitted stop
    engine.broker.open_orders = AsyncMock(return_value=[submitted])
    status_c2 = await engine.verify_broker_protective_stops([position])
    assert status_c2["AAPL"] is True
    assert engine.allow_new_entries is True


def test_two_partial_exit_fills_create_one_trade():
    tracker = PortfolioTracker()
    t0 = datetime.now(UTC)
    t1 = datetime.now(UTC)
    t2 = datetime.now(UTC)

    tracker._lots["CRDO"] = _OpenLot(
        qty=10.0, avg_price=230.70, entry_ts=t0, strategy="manual", initial_risk_per_share=2.0
    )

    fill1 = Fill(
        order_id="exit_1", symbol="CRDO", side=OrderSide.SELL, qty=5.0, price=229.89, ts=t1, strategy="manual"
    )
    trade1 = tracker.on_fill(fill1)

    # Partial exit does NOT produce a completed TradeRecord
    assert trade1 is None
    assert tracker._lots["CRDO"].qty == 5.0
    assert len(tracker.trades) == 0

    fill2 = Fill(
        order_id="exit_2", symbol="CRDO", side=OrderSide.SELL, qty=5.0, price=229.89, ts=t2, strategy="manual"
    )
    trade2 = tracker.on_fill(fill2)

    # Position fully closed -> produces 1 single logical TradeRecord
    assert trade2 is not None
    assert trade2.qty == 10.0
    assert len(tracker.trades) == 1


def test_short_position_requires_buy_stop_above_entry():
    engine = TradingEngine.__new__(TradingEngine)
    position = Position(symbol="AAOI", qty=-1.0, avg_entry_price=111.11, market_price=111.11)
    valid_stop = Order(
        id="aaoi-stop", symbol="AAOI", side=OrderSide.BUY, qty=1.0,
        order_type=OrderType.STOP, stop_price=112.78, status=OrderStatus.SUBMITTED
    )
    dangerous_limit = Order(
        id="aaoi-limit", symbol="AAOI", side=OrderSide.BUY, qty=1.0,
        order_type=OrderType.LIMIT, limit_price=112.78, status=OrderStatus.SUBMITTED
    )

    assert engine._is_valid_protective_stop(valid_stop, position) is True
    assert engine._is_valid_protective_stop(dangerous_limit, position) is False
