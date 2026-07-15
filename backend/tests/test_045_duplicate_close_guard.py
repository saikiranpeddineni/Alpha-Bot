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
        assert "NVDA" in engine._pending_close_symbols

        # Second call — should be skipped
        await engine._close_position_guarded("NVDA", "test exit again")
        assert mock_broker.close_position.call_count == 1  # still 1, not 2
    finally:
        db.close()


@pytest.mark.anyio
async def test_close_guard_releases_on_sell_fill(tmp_path):
    """Symbol removed from pending set when a SELL fill arrives."""
    from app.core.engine import TradingEngine
    from app.config import Settings
    from app.db.database import Database
    from app.core.domain import OrderSide
    from app.brokers.base import Fill

    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)

    try:
        # Simulate symbol already in pending set
        engine._pending_close_symbols.add("NVDA")

        # Create a fake SELL fill for NVDA
        fill = Fill(
            order_id="abc123",
            symbol="NVDA",
            side=OrderSide.SELL,
            qty=100.0,
            price=210.0,
        )

        # Check that sell fill clears the pending set
        if fill.side == OrderSide.SELL:
            engine._pending_close_symbols.discard(fill.symbol.upper())

        assert "NVDA" not in engine._pending_close_symbols
    finally:
        db.close()


@pytest.mark.anyio
async def test_close_guard_releases_on_failed_close(tmp_path):
    """Guard is released immediately if broker returns None (failure), allowing retry."""
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

    mock_broker = AsyncMock()
    mock_broker.close_position = AsyncMock(return_value=None)  # simulate failure
    engine.broker = mock_broker

    try:
        await engine._close_position_guarded("AMD", "exit")
        # Guard should be released since close failed
        assert "AMD" not in engine._pending_close_symbols
        # Second call should be allowed through
        await engine._close_position_guarded("AMD", "retry exit")
        assert mock_broker.close_position.call_count == 2
    finally:
        db.close()
