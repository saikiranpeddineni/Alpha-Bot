import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position
from app.core.engine import TradingEngine
from app.brokers.alpaca import AlpacaBroker
from app.portfolio.tracker import _OpenLot


@pytest.fixture
def mock_engine():
    engine = MagicMock(spec=TradingEngine)
    engine.broker = AsyncMock()
    engine.portfolio = MagicMock()
    engine.portfolio._lots = {}
    engine.allow_new_entries = True
    engine.system_health = "NORMAL"
    engine._pending_close_symbols = set()
    engine._flattening_symbols = set()
    
    # Bind real verify_broker_protective_stops and _resolve_recovery_stop methods
    engine._resolve_recovery_stop = TradingEngine._resolve_recovery_stop.__get__(engine, TradingEngine)
    engine._reconcile_protection = TradingEngine.verify_broker_protective_stops.__get__(engine, TradingEngine)
    return engine


# ---------------------------------------------------------------------------
# Domain & OrderRequest Validation Tests
# ---------------------------------------------------------------------------

def test_order_request_validation_market():
    req = OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.MARKET)
    assert req.order_type == OrderType.MARKET

    with pytest.raises(ValueError, match="MARKET orders cannot specify limit_price or stop_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.MARKET, limit_price=150.0)

    with pytest.raises(ValueError, match="MARKET orders cannot specify limit_price or stop_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.MARKET, stop_price=140.0)


def test_order_request_validation_limit():
    req = OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=150.0)
    assert req.limit_price == 150.0

    with pytest.raises(ValueError, match="LIMIT orders require limit_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT)

    with pytest.raises(ValueError, match="LIMIT orders cannot specify stop_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=150.0, stop_price=140.0)


def test_order_request_validation_stop():
    req = OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP, stop_price=140.0)
    assert req.stop_price == 140.0

    with pytest.raises(ValueError, match="STOP orders require stop_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP)

    with pytest.raises(ValueError, match="STOP orders cannot specify limit_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP, stop_price=140.0, limit_price=150.0)


def test_order_request_validation_stop_limit():
    req = OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP_LIMIT, stop_price=140.0, limit_price=139.5)
    assert req.stop_price == 140.0
    assert req.limit_price == 139.5

    with pytest.raises(ValueError, match="STOP_LIMIT orders require stop_price and limit_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP_LIMIT, stop_price=140.0)


# ---------------------------------------------------------------------------
# Alpaca Broker Adapter Contract Tests
# ---------------------------------------------------------------------------

def test_alpaca_broker_to_order_mappings():
    broker = MagicMock(spec=AlpacaBroker)
    broker._meta = {}
    broker._to_order = AlpacaBroker._to_order.__get__(broker, AlpacaBroker)

    raw_market = MagicMock(id="1", client_order_id="c1", symbol="AAPL", side="buy", qty=10, type="market", limit_price=None, stop_price=None, order_class=None, status="filled", filled_qty=10, filled_avg_price=150.0, submitted_at=datetime.now(UTC), filled_at=datetime.now(UTC))
    o_m = broker._to_order(raw_market)
    assert o_m.order_type == OrderType.MARKET
    assert o_m.status == OrderStatus.FILLED

    raw_stop = MagicMock(id="2", client_order_id="c2", symbol="CRDO", side="sell", qty=89, type="stop", limit_price=None, stop_price=227.29, order_class=None, status="new", filled_qty=0, filled_avg_price=None, submitted_at=datetime.now(UTC), filled_at=None)
    o_s = broker._to_order(raw_stop)
    assert o_s.order_type == OrderType.STOP
    assert o_s.stop_price == 227.29
    assert o_s.limit_price is None
    assert o_s.status == OrderStatus.SUBMITTED

    raw_part_fill = MagicMock(id="3", client_order_id="c3", symbol="CRDO", side="sell", qty=89, type="stop", limit_price=None, stop_price=227.29, order_class=None, status="partially_filled", filled_qty=40, filled_avg_price=None, submitted_at=datetime.now(UTC), filled_at=None)
    o_pf = broker._to_order(raw_part_fill)
    assert o_pf.status == OrderStatus.PARTIALLY_FILLED
    assert o_pf.filled_qty == 40.0

    raw_unknown_type = MagicMock(id="4", type="exotic_option")
    with pytest.raises(ValueError, match="Unsupported broker order type"):
        broker._to_order(raw_unknown_type)


# ---------------------------------------------------------------------------
# Permanent CRDO Incident Regression Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_41_crdo_recovery_is_resting_stop_not_marketable_limit(mock_engine):
    """PERMANENT INCIDENT REGRESSION:
    Long CRDO position entered at $228.61 with strategy stop at $227.29 and current market price at $227.80.
    Rule #41 MUST submit a resting SELL STOP at $227.29, NOT a marketable SELL LIMIT at $225.18.
    The position must NOT be exited while price is $227.80, and the engine must remain DEGRADED
    until broker open_orders confirms the active STOP order on cycle 2.
    """
    pos = Position(symbol="CRDO", qty=89.0, avg_entry_price=228.61, market_price=227.80)
    
    # Set portfolio lot with initial strategy stop at $227.29
    lot = _OpenLot(qty=89.0, avg_price=228.61, entry_ts=datetime.now(UTC), strategy="rsi2_reversion", stop_loss=227.29, initial_stop_loss=227.29)
    mock_engine.portfolio._lots["CRDO"] = lot

    # Cycle 1: Broker open_orders is empty (missing protection)
    mock_engine.broker.open_orders.return_value = []
    rec_order = Order(
        id="order_rec_crdo",
        client_order_id="protect-CRDO-22861-22729",
        symbol="CRDO",
        side=OrderSide.SELL,
        qty=89.0,
        order_type=OrderType.STOP,
        stop_price=227.29,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.get_order_by_client_id.return_value = None
    mock_engine.broker.submit.return_value = rec_order

    status_c1 = await mock_engine._reconcile_protection([pos])

    # Assertions for Cycle 1:
    # 1. Protection status is NOT immediately set to True on submission (awaiting broker verification)
    assert status_c1["CRDO"] is False
    assert mock_engine.system_health == "DEGRADED"
    assert mock_engine.allow_new_entries is False

    # 2. Submitted order is SELL STOP at 227.29 (NOT LIMIT) with source=ORIGINAL_STRATEGY_STOP
    mock_engine.broker.submit.assert_called_once()
    req = mock_engine.broker.submit.call_args[0][0]
    assert req.symbol == "CRDO"
    assert req.side == OrderSide.SELL
    assert req.qty == 89.0
    assert req.order_type == OrderType.STOP
    assert req.stop_price == pytest.approx(227.29)
    assert req.limit_price is None
    assert "source=ORIGINAL_STRATEGY_STOP" in req.reason

    # Cycle 2: Broker open_orders now returns the active SELL STOP order
    active_stop = Order(
        id="order_rec_crdo",
        client_order_id="protect-CRDO-22861-22729",
        symbol="CRDO",
        side=OrderSide.SELL,
        qty=89.0,
        order_type=OrderType.STOP,
        stop_price=227.29,
        stop_loss=227.29,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.open_orders.return_value = [active_stop]

    status_c2 = await mock_engine._reconcile_protection([pos])

    # Assertions for Cycle 2:
    assert status_c2["CRDO"] is True
    assert mock_engine.system_health == "NORMAL"
    assert mock_engine.allow_new_entries is True


@pytest.mark.asyncio
async def test_41_short_position_recovery_stop(mock_engine):
    """Short position (SHORT 100 shares @ $150.00, market @ $151.00) recovers a BUY STOP at $152.50."""
    pos = Position(symbol="XYZ", qty=-100.0, avg_entry_price=150.00, market_price=151.00)
    lot = _OpenLot(qty=-100.0, avg_price=150.00, entry_ts=datetime.now(UTC), strategy="momentum", stop_loss=152.50, initial_stop_loss=152.50)
    mock_engine.portfolio._lots["XYZ"] = lot

    mock_engine.broker.open_orders.return_value = []
    mock_engine.broker.get_order_by_client_id.return_value = None

    status = await mock_engine._reconcile_protection([pos])
    assert status["XYZ"] is False
    assert mock_engine.system_health == "DEGRADED"

    mock_engine.broker.submit.assert_called_once()
    req = mock_engine.broker.submit.call_args[0][0]
    assert req.symbol == "XYZ"
    assert req.side == OrderSide.BUY
    assert req.qty == 100.0
    assert req.order_type == OrderType.STOP
    assert req.stop_price == pytest.approx(152.50)
    assert req.limit_price is None


@pytest.mark.asyncio
async def test_41_fallback_to_1_5pct_when_no_strategy_stop(mock_engine):
    """When no initial strategy stop is available in portfolio lot, recovery falls back to 1.5% stop."""
    pos = Position(symbol="TSLA", qty=10.0, avg_entry_price=200.00, market_price=200.00)
    mock_engine.portfolio._lots = {}  # No lot

    mock_engine.broker.open_orders.return_value = []
    mock_engine.broker.get_order_by_client_id.return_value = None

    status = await mock_engine._reconcile_protection([pos])
    assert status["TSLA"] is False

    mock_engine.broker.submit.assert_called_once()
    req = mock_engine.broker.submit.call_args[0][0]
    assert req.order_type == OrderType.STOP
    assert req.stop_price == pytest.approx(200.00 * 0.985)
    assert "source=EMERGENCY_1_5PCT_FALLBACK" in req.reason


@pytest.mark.asyncio
async def test_41_pre_submission_geometry_failure_aborts(mock_engine):
    """Pre-submission geometry validation aborts recovery if calculated stop_px is above market price for LONG."""
    # Long position where calculated stop (e.g. 230.0) is >= market price (227.80)
    pos = Position(symbol="BAD", qty=10.0, avg_entry_price=233.50, market_price=227.80)
    lot = _OpenLot(qty=10.0, avg_price=233.50, entry_ts=datetime.now(UTC), strategy="momentum", stop_loss=230.0, initial_stop_loss=230.0)
    mock_engine.portfolio._lots["BAD"] = lot

    mock_engine.broker.open_orders.return_value = []

    status = await mock_engine._reconcile_protection([pos])

    # Geometry failure prevents submission
    assert status["BAD"] is False
    assert mock_engine.system_health == "DEGRADED"
    assert mock_engine.allow_new_entries is False
    mock_engine.broker.submit.assert_not_called()


@pytest.mark.asyncio
async def test_41_standalone_tp_cancelled_before_stop_recovery(mock_engine):
    """Standalone TP (SELL @ $169.90 for SKHY @ $168.40) is cancelled on cycle 1 before submitting recovery stop."""
    pos = Position(symbol="SKHY", qty=121.0, avg_entry_price=168.40, market_price=168.40)
    tp_order = Order(
        id="order_tp_1",
        client_order_id="cid_tp_1",
        symbol="SKHY",
        side=OrderSide.SELL,
        qty=121.0,
        order_type=OrderType.LIMIT,
        limit_price=169.90,
        status=OrderStatus.SUBMITTED,
    )
    mock_engine.broker.open_orders.return_value = [tp_order]

    status = await mock_engine._reconcile_protection([pos])

    assert status["SKHY"] is False
    assert mock_engine.system_health == "DEGRADED"
    assert mock_engine.allow_new_entries is False
    
    mock_engine.broker.cancel.assert_called_once_with("order_tp_1")
    mock_engine.broker.submit.assert_not_called()
