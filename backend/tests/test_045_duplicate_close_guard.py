"""Tests for issue 045 — duplicate close-order loop guard."""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.anyio
async def test_close_guard_skips_second_call(tmp_path):
    """Second close call for same symbol is skipped while first is in-flight."""
    from app.core.engine import TradingEngine
    from app.config import Settings
    from app.db.database import Database

    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)

    # Mock broker
    mock_broker = AsyncMock()
    mock_broker.close_position = AsyncMock(return_value=MagicMock())  # returns an Order
    engine.broker = mock_broker

    try:
        # First call — should go through
        await engine._close_position_guarded("NVDA", "test exit")
        assert mock_broker.close_position.call_count == 1
        assert "NVDA" in engine._pending_close_ttls

        # Second call — should be skipped
        await engine._close_position_guarded("NVDA", "test exit again")
        assert mock_broker.close_position.call_count == 1  # still 1, not 2
    finally:
        db.close()


@pytest.mark.anyio
async def test_close_guard_releases_on_sell_fill(tmp_path):
    """Symbol removed from pending TTL set when a SELL fill arrives."""
    from app.core.engine import TradingEngine
    from app.config import Settings
    from app.db.database import Database
    from app.core.domain import OrderSide, OrderStatus, OrderType, Order
    from app.brokers.base import Fill
    import time

    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)
    
    # Mock broker to return a fill
    mock_broker = AsyncMock()
    
    fake_order = Order(
        id="abc1234",
        symbol="NVDA",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        qty=100.0,
        status=OrderStatus.FILLED,
        filled_qty=100.0,
        filled_avg_price=210.0,
    )
    
    fake_fill = Fill(
        order_id="abc1234",
        symbol="NVDA",
        side=OrderSide.SELL,
        qty=100.0,
        price=210.0,
    )
    
    mock_broker.drain_fills = MagicMock(return_value=[fake_fill])
    # The _drain_fills also fetches the order and updates portfolio
    mock_broker.fetch_order = AsyncMock(return_value=fake_order)
    engine.broker = mock_broker

    try:
        # Simulate symbol already in pending TTL set (e.g. close initiated)
        engine._pending_close_ttls = {"NVDA": time.time() + 30.0}
        
        # We need the portfolio to not have the lot anymore to allow the guard to clear
        engine.portfolio = MagicMock()
        engine.portfolio._lots = {}
        engine.portfolio.on_fill.return_value = None
        engine.risk = MagicMock()

        # Process the fill
        await engine._drain_fills(time.time())

        # Check that sell fill cleared the pending TTL set
        assert "NVDA" not in getattr(engine, "_pending_close_ttls", {})
    finally:
        db.close()


@pytest.mark.anyio
async def test_close_guard_releases_on_failed_close(tmp_path):
    """Guard is retained for TTL even if broker returns None (failure), to prevent ambiguous resubmissions."""
    from app.core.engine import TradingEngine
    from app.config import Settings
    from app.db.database import Database
    import time

    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)

    mock_broker = AsyncMock()
    mock_broker.close_position = AsyncMock(return_value=None)  # simulate failure
    engine.broker = mock_broker

    try:
        await engine._close_position_guarded("AMD", "exit")
        # Guard should be retained for 30s
        assert "AMD" in engine._pending_close_ttls
        # Second call should be skipped
        await engine._close_position_guarded("AMD", "retry exit")
        assert mock_broker.close_position.call_count == 1
        
        # If TTL expires, second call is allowed
        engine._pending_close_ttls["AMD"] = time.time() - 10
        await engine._close_position_guarded("AMD", "retry exit")
        assert mock_broker.close_position.call_count == 2
    finally:
        db.close()
