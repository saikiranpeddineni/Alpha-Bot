from datetime import UTC, datetime, timedelta

import pytest

from app.core.domain import Quote
from app.core.universe_discovery import DiscoverableAsset, DiscoveryCoordinator


class CountingProvider:
    name = "counting"
    supports_asset_discovery = True

    def __init__(self):
        self.asset_calls = 0
        self.quote_calls = []

    async def discover_assets(self):
        self.asset_calls += 1
        return tuple(
            DiscoverableAsset(symbol, True, True)
            for symbol in ("DDD", "aaa", "CCC", "BBB", "AAA")
        )

    async def get_quotes(self, symbols):
        self.quote_calls.append(tuple(symbols))
        now = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        return {
            symbol: Quote(symbol=symbol, price=100.0, change_pct=1.0, volume=1000.0, ts=now)
            for symbol in symbols
        }


@pytest.mark.asyncio
async def test_discovery_caches_assets_deduplicates_and_batches_with_identity_telemetry():
    provider = CountingProvider()
    now = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    coordinator = DiscoveryCoordinator(
        provider,
        feed="iex",
        asset_cache_ttl=timedelta(hours=1),
        batch_size=2,
        request_budget=10,
        clock=lambda: now,
    )

    first = await coordinator.acquire()
    second = await coordinator.acquire()

    assert provider.asset_calls == 1
    assert provider.quote_calls == [
        ("AAA", "BBB"), ("CCC", "DDD"),
        ("AAA", "BBB"), ("CCC", "DDD"),
    ]
    assert first.telemetry.provider == "counting"
    assert first.telemetry.feed == "iex"
    assert first.telemetry.asset_cache_hit is False
    assert second.telemetry.asset_cache_hit is True
    assert first.telemetry.eligible_symbols == 4
    assert first.telemetry.snapshot_requests == 2
    assert first.telemetry.covered_symbols == 4
    assert first.telemetry.missing_symbols == 0


@pytest.mark.asyncio
async def test_discovery_request_budget_stops_before_unbounded_snapshot_scan():
    provider = CountingProvider()
    coordinator = DiscoveryCoordinator(
        provider,
        feed="iex",
        asset_cache_ttl=timedelta(hours=1),
        batch_size=2,
        request_budget=1,
    )

    result = await coordinator.acquire()

    assert provider.quote_calls == [("AAA", "BBB")]
    assert set(result.quotes) == {"AAA", "BBB"}
    assert result.telemetry.snapshot_requests == 1
    assert result.telemetry.request_budget == 1
    assert result.telemetry.covered_symbols == 2
    assert result.telemetry.missing_symbols == 2
    assert result.telemetry.budget_exhausted is True


@pytest.mark.asyncio
async def test_discovery_ignores_out_of_batch_provider_records():
    provider = CountingProvider()

    async def polluted_quotes(symbols):
        provider.quote_calls.append(tuple(symbols))
        now = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        return {
            "AAPL": Quote(symbol="AAPL", price=100.0, change_pct=1.0, volume=1000.0, ts=now),
            "INJECTED": Quote(symbol="INJECTED", price=999.0, change_pct=99.0, volume=999999.0, ts=now),
        }

    provider.get_quotes = polluted_quotes
    coordinator = DiscoveryCoordinator(
        provider,
        feed="iex",
        asset_cache_ttl=timedelta(hours=1),
        batch_size=2,
        request_budget=1,
    )

    result = await coordinator.acquire()

    assert result.quotes == {}
    assert result.telemetry.covered_symbols == 0
    assert result.telemetry.missing_symbols == 4


@pytest.mark.asyncio
async def test_discovery_cache_expiry_refreshes_asset_metadata():
    provider = CountingProvider()
    current = [datetime(2026, 7, 15, 14, 0, tzinfo=UTC)]
    coordinator = DiscoveryCoordinator(
        provider,
        feed="iex",
        asset_cache_ttl=timedelta(minutes=30),
        batch_size=10,
        request_budget=1,
        clock=lambda: current[0],
    )

    await coordinator.acquire()
    current[0] += timedelta(minutes=31)
    result = await coordinator.acquire()

    assert provider.asset_calls == 2
    assert result.telemetry.asset_cache_hit is False


def test_discovery_configuration_rejects_unbounded_or_invalid_limits():
    provider = CountingProvider()
    with pytest.raises(ValueError, match="batch_size must be positive"):
        DiscoveryCoordinator(provider, feed="iex", asset_cache_ttl=timedelta(0), batch_size=0, request_budget=1)
    with pytest.raises(ValueError, match="request_budget must be positive"):
        DiscoveryCoordinator(provider, feed="iex", asset_cache_ttl=timedelta(0), batch_size=1, request_budget=0)
    with pytest.raises(ValueError, match="asset_cache_ttl must be non-negative"):
        DiscoveryCoordinator(provider, feed="iex", asset_cache_ttl=timedelta(seconds=-1), batch_size=1, request_budget=1)
