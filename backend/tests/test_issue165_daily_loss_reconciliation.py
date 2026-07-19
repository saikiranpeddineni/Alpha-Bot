import pytest
import asyncio
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock
from app.core.engine import TradingEngine, EngineState
from app.core.domain import Position, Order, OrderStatus, OrderSide, OrderType
from app.risk.manager import AccountContext
from app.brokers.base import BrokerTimeoutError
from app.config import Settings

@pytest.mark.asyncio
async def test_daily_loss_reconciliation_retry_loop():
    # Setup mock broker, risk manager, data feed, etc.
    broker = MagicMock()
    broker.open_orders = AsyncMock()
    broker.positions = AsyncMock()
    broker.cancel_all = AsyncMock()
    broker.flatten_all = AsyncMock()
    broker.account = AsyncMock()
    broker.poll_fills = AsyncMock()

    data = MagicMock()
    data.poll = AsyncMock(return_value={})
    db = MagicMock()
    db.get_setting.return_value = None
    risk = MagicMock()
    clock = MagicMock()

    settings = Settings()
    # Create TradingEngine
    engine = TradingEngine(settings=settings, db=db)
    engine.broker = broker
    engine.risk = risk
    engine.clock = clock
    engine.data = data
    engine.mode = "demo"
    engine.state = EngineState.RUNNING

    ctx = AccountContext(equity=100000.0, cash=50000.0, positions=[], day_pnl=-5000.0, session_start_equity=100000.0, peak_equity=100000.0, day_trades_today=0)
    portfolio = MagicMock()
    portfolio.context.return_value = ctx
    portfolio.day_pnl.return_value = -5000.0
    portfolio.session_start_equity = 100000.0
    portfolio.peak_equity = 100000.0
    portfolio.day_trades_today = 0
    portfolio.day_trades_in_window.return_value = 0
    portfolio.roll_session.return_value = False
    pipeline = MagicMock()

    broker.is_paper = False
    engine.broker = broker
    engine.risk = risk
    engine.clock = clock
    engine.data = data
    engine.portfolio = portfolio
    engine.pipeline = pipeline
    engine.mode = "demo"
    engine.state = EngineState.RUNNING

    # Trip the daily loss halt
    risk.check_halts.return_value = "daily"
    risk.should_flatten.return_value = False
    risk.daily_halted = True
    risk.hard_halted = False

    # First tick: broker raises timeout exception on cancel_all
    broker.open_orders.return_value = [Order(id="ord_1", symbol="AAPL", side=OrderSide.BUY, qty=10, status=OrderStatus.PENDING, order_type=OrderType.MARKET)]
    broker.positions.return_value = []
    broker.cancel_all.side_effect = BrokerTimeoutError("ALPACA TIMEOUT")

    # Trigger engine tick
    await engine._tick()

    # Verify that cancel_all was called
    broker.cancel_all.assert_called_once()
    # Engine must be in unresolved Daily Loss halt state
    assert "DAILY LOSS HALTED — liquidation unresolved: BrokerTimeoutError: ALPACA TIMEOUT" in engine.last_error

    # Second tick: cancel_all succeeds, but flatten_all raises General Exception
    broker.cancel_all.side_effect = None
    broker.open_orders.return_value = []
    broker.positions.return_value = [Position(symbol="AAPL", avg_entry_price=100.0, qty=10, market_price=100.0, unrealized_pnl=0.0, realized_pnl=0.0)]
    broker.flatten_all.side_effect = Exception("ALPACA FAIL")

    # Reset mock call counts
    broker.cancel_all.reset_mock()
    broker.flatten_all.reset_mock()

    risk.check_halts.return_value = None  # Daily halt is already active

    await engine._tick()

    # Verify flatten_all was called
    broker.flatten_all.assert_called_once_with(reason="daily loss halt")
    # Verify daily halt liquidation is still unresolved with new error
    assert "DAILY LOSS HALTED — liquidation unresolved: Exception: ALPACA FAIL" in engine.last_error

    # Third tick: both succeed, broker returns flat state
    broker.flatten_all.side_effect = None
    broker.positions.return_value = [] # flat!
    broker.open_orders.return_value = [] # flat!

    broker.flatten_all.reset_mock()

    await engine._tick()

    # Should authorize flat successfully
    assert engine.last_error == ""
