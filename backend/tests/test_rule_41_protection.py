import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.core.domain import Order, OrderRequest, OrderSide, OrderStatus, OrderType, Position, ProtectionState
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

    with pytest.raises(ValueError, match="LIMIT requires positive limit_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT)

    with pytest.raises(ValueError, match="LIMIT orders cannot specify stop_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=150.0, stop_price=140.0)


def test_order_request_validation_stop():
    req = OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP, stop_price=140.0)
    assert req.stop_price == 140.0

    with pytest.raises(ValueError, match="STOP requires positive stop_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP)

    with pytest.raises(ValueError, match="STOP orders cannot specify limit_price"):
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP, stop_price=140.0, limit_price=150.0)


def test_order_request_validation_stop_limit():
    req = OrderRequest(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.STOP_LIMIT, stop_price=140.0, limit_price=139.5)
    assert req.stop_price == 140.0
    assert req.limit_price == 139.5

    with pytest.raises(ValueError, match="STOP_LIMIT requires positive limit_price"):
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




