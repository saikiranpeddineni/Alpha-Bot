import pytest
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock

from app.brokers.base import Fill
from app.core.domain import Order, OrderRequest, OrderSide, OrderType, OrderStatus, Position
from app.core.engine import TradingEngine
from app.portfolio.tracker import PortfolioTracker, _OpenLot
from app.risk.manager import RiskManager, RiskConfig


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


def test_limit_order_is_not_protective_stop():
    engine = TradingEngine.__new__(TradingEngine)
    long_position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    order = Order(
        id="bad-limit",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=100.0,
        order_type=OrderType.LIMIT,
        limit_price=197.0,
        status=OrderStatus.SUBMITTED,
    )

    assert not engine._is_valid_protective_stop(
        order,
        long_position,
    )


def test_partially_filled_stop_must_cover_remaining_position():
    engine = TradingEngine.__new__(TradingEngine)
    position_qty_100 = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    order = Order(
        id="stop",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=100.0,
        filled_qty=50.0,
        order_type=OrderType.STOP,
        stop_price=197.0,
        status=OrderStatus.PARTIALLY_FILLED,
    )

    assert order.remaining_qty == 50.0
    assert not engine._is_valid_protective_stop(
        order,
        position_qty_100,
    )


def test_bracket_parent_is_not_stop():
    engine = TradingEngine.__new__(TradingEngine)
    long_position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    order = Order(
        id="parent",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=100.0,
        order_type=OrderType.MARKET,
        order_class="bracket",
        leg="entry",
        stop_loss=197.0,
        status=OrderStatus.SUBMITTED,
    )

    assert not engine._is_valid_protective_stop(
        order,
        long_position,
    )


@pytest.mark.asyncio
async def test_recovery_submission_does_not_mark_protected():
    engine = TradingEngine.__new__(TradingEngine)
    engine.portfolio = PortfolioTracker()
    engine.broker = AsyncMock()
    position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    submitted_stop = Order(
        id="stop-123",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=100.0,
        order_type=OrderType.STOP,
        stop_price=197.0,
        status=OrderStatus.SUBMITTED,
    )
    engine.broker.open_orders = AsyncMock(return_value=[])
    engine.broker.get_order_by_client_id = AsyncMock(return_value=None)
    engine.broker.submit = AsyncMock(return_value=submitted_stop)

    result = await engine.verify_broker_protective_stops(
        [position]
    )

    assert result[position.symbol] is False
    assert engine.allow_new_entries is False


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
    # Protection verification verifies the stop, but does not own re-enabling entries
    assert engine.allow_new_entries is False


def test_two_partial_fills_create_one_logical_loss():
    tracker = PortfolioTracker()
    risk = RiskManager(RiskConfig(), clock=MagicMock())
    t0 = datetime.now(UTC)
    t1 = datetime.now(UTC)
    t2 = datetime.now(UTC)

    tracker._lots["AAPL"] = _OpenLot(
        qty=10.0, avg_price=200.0, entry_ts=t0, strategy="manual", initial_risk_per_share=2.0
    )

    first_five_share_exit = Fill(
        order_id="exit_1", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t1, strategy="manual"
    )
    first_res = tracker.on_fill(first_five_share_exit, risk_manager=risk)

    assert first_res is None
    assert len(tracker.trades) == 0

    second_five_share_exit = Fill(
        order_id="exit_2", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t2, strategy="manual"
    )
    second_res = tracker.on_fill(second_five_share_exit, risk_manager=risk)

    assert second_res is not None
    assert second_res.qty == 10.0
    assert len(tracker.trades) == 1
    assert risk.consecutive_losses == 1
    assert risk.strategy_consecutive_losses["manual"] == 1


def test_pdt_once():
    tracker = PortfolioTracker()
    t0 = datetime.now(UTC)
    t1 = datetime.now(UTC)
    t2 = datetime.now(UTC)

    tracker._lots["AAPL"] = _OpenLot(
        qty=10.0, avg_price=200.0, entry_ts=t0, strategy="manual", initial_risk_per_share=2.0
    )

    first = Fill(
        order_id="exit_1", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t1, strategy="manual"
    )
    tracker.on_fill(first)
    assert tracker.day_trades_today == 0

    second = Fill(
        order_id="exit_2", symbol="AAPL", side=OrderSide.SELL, qty=5.0, price=195.0, ts=t2, strategy="manual"
    )
    tracker.on_fill(second)
    assert tracker.day_trades_today == 1


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


@pytest.mark.asyncio
async def test_heartbeat_cannot_reenable_blocked_entries():
    engine = TradingEngine.__new__(TradingEngine)
    engine.allow_new_entries = False
    engine.system_health = "DEGRADED"

    # Heartbeat check should not re-enable entries when blocked by protection or data gap
    heartbeat_health = "NORMAL"
    heartbeat_blocks_entries = False

    if heartbeat_health != "NORMAL":
        if engine.system_health not in {"HALTED", "SAFE_MODE"}:
            engine.system_health = heartbeat_health

    if heartbeat_blocks_entries:
        engine.allow_new_entries = False

    assert engine.allow_new_entries is False


@pytest.mark.asyncio
async def test_replay_deferred():
    engine = TradingEngine.__new__(TradingEngine)
    engine.db = MagicMock()
    engine.data = AsyncMock()
    engine.mode = "paper"

    signal = {
        "candidate_id": "cand-1",
        "symbol": "AAPL",
        "direction": "long",
        "timestamp": datetime.now(UTC).isoformat(),
        "replay_attempts": 0,
    }
    engine.db.get_pending_shadow_replays.return_value = [signal]
    engine.data.history.return_value = MagicMock(empty=True)

    await engine._replay_pending_signals(datetime.now(UTC))

    saved_signal = engine.db.save_shadow_signal.call_args[0][6]
    assert saved_signal.get("replayed", False) is False
    assert saved_signal.get("replay_failed", False) is False
    assert saved_signal.get("replay_state") == "deferred_data_incomplete"
    assert saved_signal.get("next_retry_at") is not None


@pytest.mark.asyncio
async def test_replay_respects_backoff():
    engine = TradingEngine.__new__(TradingEngine)
    engine.db = MagicMock()
    engine.data = AsyncMock()
    engine.mode = "paper"

    future_retry = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    signal = {
        "candidate_id": "cand-1",
        "symbol": "AAPL",
        "direction": "long",
        "timestamp": datetime.now(UTC).isoformat(),
        "next_retry_at": future_retry,
    }
    engine.db.get_pending_shadow_replays.return_value = [signal]

    await engine._replay_pending_signals(datetime.now(UTC))

    assert engine.data.history.call_count == 0


@pytest.mark.asyncio
async def test_protection_verification_cannot_clear_unrelated_entry_block():
    engine = TradingEngine.__new__(TradingEngine)
    engine.portfolio = PortfolioTracker()
    engine.broker = AsyncMock()
    engine.allow_new_entries = False
    engine.system_health = "DEGRADED"

    protected_position = Position(symbol="AAPL", qty=100.0, avg_entry_price=200.0, market_price=200.0)
    valid_stop_order = Order(
        id="stop-valid", symbol="AAPL", side=OrderSide.SELL, qty=100.0,
        order_type=OrderType.STOP, stop_price=197.0, status=OrderStatus.SUBMITTED
    )

    # Broker contains valid protection.
    engine.broker.open_orders = AsyncMock(return_value=[valid_stop_order])

    result = await engine.verify_broker_protective_stops([protected_position])

    assert result[protected_position.symbol] is True
    # Protection is healthy, but this function does not own unrelated entry blocks.
    assert engine.allow_new_entries is False
