"""Tests for issue 049 — scanner shortlist caching."""
from __future__ import annotations
import pytest
import asyncio
from datetime import timedelta, datetime, UTC
from unittest.mock import AsyncMock, MagicMock
from app.core.universe_discovery import DiscoveryCoordinator, DiscoverableAsset, DiscoveryResult
from app.core.domain import Quote


@pytest.mark.anyio
async def test_shortlist_cache_hit_prevents_api_calls():
    """Subsequent acquire calls within TTL return cached result and skip API requests."""
    # Mock provider
    mock_provider = MagicMock()
    mock_provider.name = "mock_provider"
    mock_provider.supports_asset_discovery = True

    # 2 assets: A and B
    assets = (
        DiscoverableAsset(symbol="A", active=True, tradable=True),
        DiscoverableAsset(symbol="B", active=True, tradable=True),
    )
    mock_provider.discover_assets = AsyncMock(return_value=assets)

    # Mock quotes
    quotes = {
        "A": Quote(symbol="A", price=10.0, change_pct=1.0, volume=1000.0, ts=datetime.now(UTC)),
        "B": Quote(symbol="B", price=20.0, change_pct=2.0, volume=2000.0, ts=datetime.now(UTC)),
    }
    mock_provider.get_quotes = AsyncMock(return_value=quotes)

    # Initialize coordinator with 6 hours cache TTL
    coordinator = DiscoveryCoordinator(
        mock_provider,
        feed="iex",
        asset_cache_ttl=timedelta(hours=6),
        batch_size=200,
        request_budget=10,
    )

    # First call — should perform API queries (cache miss)
    result1 = await coordinator.acquire()
    assert len(result1.shortlist) == 2
    assert mock_provider.discover_assets.call_count == 1
    assert mock_provider.get_quotes.call_count == 1
    assert result1.telemetry.asset_cache_hit is False

    # Second call — should hit cache and skip API queries completely
    result2 = await coordinator.acquire()
    assert len(result2.shortlist) == 2
    assert result2.shortlist == result1.shortlist
    assert mock_provider.discover_assets.call_count == 1  # unchanged
    assert mock_provider.get_quotes.call_count == 1       # unchanged
    assert result2.telemetry.asset_cache_hit is True


@pytest.mark.anyio
async def test_scanner_persists_discovery_on_datamanager():
    """Verify that multiple transient MarketScanner runs reuse the same coordinator via DataManager."""
    from app.core.scanner import MarketScanner
    from app.data.manager import DataManager

    # Mock provider
    mock_provider = MagicMock()
    mock_provider.name = "mock_provider"
    mock_provider.supports_asset_discovery = True

    assets = (
        DiscoverableAsset(symbol="A", active=True, tradable=True),
        DiscoverableAsset(symbol="B", active=True, tradable=True),
    )
    mock_provider.discover_assets = AsyncMock(return_value=assets)

    quotes = {
        "A": Quote(symbol="A", price=10.0, change_pct=1.0, volume=1000.0, ts=datetime.now(UTC)),
        "B": Quote(symbol="B", price=20.0, change_pct=2.0, volume=2000.0, ts=datetime.now(UTC)),
    }
    mock_provider.get_quotes = AsyncMock(return_value=quotes)
    mock_provider.close = AsyncMock()

    # Initialize a real DataManager
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        data_manager = DataManager(mock_provider, Path(tmp) / "test.db")
        # Run Scanner 1
        scanner1 = MarketScanner(data_manager, ["SPY"])
        symbols1 = await scanner1._stage1_symbols()
        assert len(symbols1) == 2
        assert mock_provider.discover_assets.call_count == 1
        assert mock_provider.get_quotes.call_count == 1

        # Run Scanner 2 (new instance, same DataManager)
        scanner2 = MarketScanner(data_manager, ["SPY"])
        symbols2 = await scanner2._stage1_symbols()
        assert len(symbols2) == 2
        assert mock_provider.discover_assets.call_count == 1  # unchanged
        assert mock_provider.get_quotes.call_count == 1       # unchanged
        await data_manager.close()


@pytest.mark.anyio
async def test_daily_deterministic_shuffling():
    """Verify that symbols are shuffled differently on new days, but remain stable on the same day."""
    mock_provider = MagicMock()
    mock_provider.name = "mock_provider"
    mock_provider.supports_asset_discovery = True

    # 10 assets
    assets = tuple(DiscoverableAsset(symbol=f"S{i}", active=True, tradable=True) for i in range(10))
    mock_provider.discover_assets = AsyncMock(return_value=assets)

    # Dynamic quotes generator with matching timestamps
    current_test_time = datetime.now(UTC)
    async def mock_get_quotes(symbols, **kwargs):
        return {
            sym: Quote(symbol=sym, price=10.0, change_pct=1.0, volume=1000.0, ts=current_test_time)
            for sym in symbols
        }
    mock_provider.get_quotes = mock_get_quotes

    # Coordinator 1: Day 1 (ts: 2026-07-15 10:00:00 UTC)
    t1 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    current_test_time = t1
    coord1 = DiscoveryCoordinator(mock_provider, feed="iex", asset_cache_ttl=timedelta(0), batch_size=2, request_budget=10, clock=lambda: t1)
    res1_run1 = await coord1.acquire()

    # Same day, second run (cache expired by ttl=0 but daily seed is the same)
    t2 = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)
    current_test_time = t2
    coord2 = DiscoveryCoordinator(mock_provider, feed="iex", asset_cache_ttl=timedelta(0), batch_size=2, request_budget=10, clock=lambda: t2)
    res1_run2 = await coord2.acquire()

    # Verification 1: Same day shuffling is identical (meaning identical query order)
    assert len(res1_run1.quotes) > 0
    assert list(res1_run1.quotes.keys()) == list(res1_run2.quotes.keys())

    # Coordinator 2: Day 2 (ts: 2026-07-16 10:00:00 UTC)
    t3 = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    current_test_time = t3
    coord3 = DiscoveryCoordinator(mock_provider, feed="iex", asset_cache_ttl=timedelta(0), batch_size=2, request_budget=10, clock=lambda: t3)
    res2 = await coord3.acquire()

    # Verification 2: Different day shuffling is different (shuffled processing order is different)
    assert len(res2.quotes) > 0
    assert list(res1_run1.quotes.keys()) != list(res2.quotes.keys())
