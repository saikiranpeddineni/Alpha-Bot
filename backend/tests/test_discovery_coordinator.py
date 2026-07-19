from datetime import UTC, datetime, timedelta

import pytest

from app.core.domain import Quote
from unittest.mock import MagicMock, patch
from app.core.universe_discovery import DiscoverableAsset, DiscoveryCoordinator

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mock_rng_shuffle():
    with patch("random.Random") as mock_random:
        mock_rng = MagicMock()
        mock_rng.shuffle = lambda x: None  # no-op shuffle
        mock_random.return_value = mock_rng
        yield


class CountingProvider:
    name = "counting"
    supports_asset_discovery = True

    def __init__(self):
        self.asset_calls = 0
        self.quote_calls = []

    async def discover_assets(self):
        self.asset_calls += 1
        return tuple(DiscoverableAsset(symbol, True, True) for symbol in ("DDD", "aaa", "CCC", "BBB", "AAA"))

    async def get_quotes(self, symbols):
        self.quote_calls.append(tuple(symbols))
        return {
            symbol: Quote(symbol=symbol, price=100.0, change_pct=1.0, volume=1000.0, ts=NOW)
            for symbol in symbols
        }


def coordinator(provider, **overrides):
    kwargs = dict(
        feed="iex",
        asset_cache_ttl=timedelta(hours=1),
        batch_size=2,
        request_budget=10,
        clock=lambda: NOW,
    )
    kwargs.update(overrides)
    return DiscoveryCoordinator(provider, **kwargs)


@pytest.mark.asyncio
async def test_discovery_caches_assets_deduplicates_and_batches_with_identity_telemetry():
    provider = CountingProvider()
    discovery = coordinator(provider)

    first = await discovery.acquire()
    second = await discovery.acquire()

    assert provider.asset_calls == 1
    assert provider.quote_calls == [("AAA", "BBB"), ("CCC", "DDD")]
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
    result = await coordinator(provider, request_budget=1).acquire()

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
        return {
            "AAPL": Quote(symbol="AAPL", price=100.0, change_pct=1.0, volume=1000.0, ts=NOW),
            "INJECTED": Quote(symbol="INJECTED", price=999.0, change_pct=99.0, volume=999999.0, ts=NOW),
        }

    provider.get_quotes = polluted_quotes
    result = await coordinator(provider, request_budget=1).acquire()

    assert result.quotes == {}
    assert result.shortlist == ()
    assert result.telemetry.covered_symbols == 0
    assert result.telemetry.missing_symbols == 4


@pytest.mark.asyncio
async def test_discovery_cache_expiry_refreshes_asset_metadata():
    provider = CountingProvider()
    current = [NOW]
    discovery = DiscoveryCoordinator(
        provider,
        feed="iex",
        asset_cache_ttl=timedelta(minutes=30),
        batch_size=10,
        request_budget=1,
        clock=lambda: current[0],
    )

    await discovery.acquire()
    current[0] += timedelta(minutes=31)
    result = await discovery.acquire()

    assert provider.asset_calls == 2
    assert result.telemetry.asset_cache_hit is False


@pytest.mark.asyncio
async def test_discovery_rejects_stale_future_and_missing_snapshots_before_ranking():
    provider = CountingProvider()

    async def mixed_quotes(symbols):
        provider.quote_calls.append(tuple(symbols))
        records = {
            "AAA": Quote(symbol="AAA", price=100.0, change_pct=1.0, volume=1000.0, ts=NOW),
            "BBB": Quote(symbol="BBB", price=999.0, change_pct=99.0, volume=999999.0, ts=NOW - timedelta(minutes=6)),
            "CCC": Quote(symbol="CCC", price=999.0, change_pct=99.0, volume=999999.0, ts=NOW + timedelta(seconds=1)),
        }
        return {symbol: records[symbol] for symbol in symbols if symbol in records}

    provider.get_quotes = mixed_quotes
    result = await coordinator(provider, batch_size=10).acquire()

    assert result.shortlist == ("AAA",)
    assert set(result.quotes) == {"AAA"}
    assert result.telemetry.stale_symbols == 1
    assert result.telemetry.future_symbols == 1
    assert result.telemetry.missing_symbols == 1


@pytest.mark.asyncio
async def test_discovery_shortlist_is_deterministic_and_hard_capped():
    provider = CountingProvider()

    async def ranked_quotes(symbols):
        provider.quote_calls.append(tuple(symbols))
        values = {
            "AAA": (100.0, 1.0, 1000.0),
            "BBB": (50.0, 8.0, 3000.0),
            "CCC": (200.0, 2.0, 500.0),
            "DDD": (100.0, 9.0, 1000.0),
        }
        return {
            symbol: Quote(symbol=symbol, price=price, change_pct=change, volume=volume, ts=NOW)
            for symbol in symbols
            for price, change, volume in (values[symbol],)
        }

    provider.get_quotes = ranked_quotes
    result = await coordinator(provider, batch_size=10, shortlist_limit=2).acquire()

    assert result.shortlist == ("BBB", "DDD")
    assert result.telemetry.shortlist_symbols == 2
    assert len(result.shortlist) == 2


def test_discovery_configuration_rejects_unbounded_or_invalid_limits():
    provider = CountingProvider()
    with pytest.raises(ValueError, match="batch_size must be positive"):
        coordinator(provider, batch_size=0)
    with pytest.raises(ValueError, match="request_budget must be positive"):
        coordinator(provider, request_budget=0)
    with pytest.raises(ValueError, match="asset_cache_ttl must be non-negative"):
        coordinator(provider, asset_cache_ttl=timedelta(seconds=-1))
    with pytest.raises(ValueError, match="max_snapshot_age must be non-negative"):
        coordinator(provider, max_snapshot_age=timedelta(seconds=-1))
    with pytest.raises(ValueError, match="future_tolerance must be non-negative"):
        coordinator(provider, future_tolerance=timedelta(seconds=-1))
    with pytest.raises(ValueError, match="shortlist_limit must be positive"):
        coordinator(provider, shortlist_limit=0)
