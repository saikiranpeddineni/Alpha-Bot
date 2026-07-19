from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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


class ProviderError(RuntimeError):
    def __init__(self, status_code, retry_after=None):
        super().__init__(f"provider status {status_code}")
        headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
        self.response = SimpleNamespace(status_code=status_code, headers=headers)


class RetryProvider:
    name = "retry-provider"
    supports_asset_discovery = True

    def __init__(self, failures):
        self.failures = list(failures)
        self.quote_calls = []

    async def discover_assets(self):
        return tuple(DiscoverableAsset(symbol, True, True) for symbol in ("AAA", "BBB", "CCC", "DDD"))

    async def get_quotes(self, symbols):
        self.quote_calls.append(tuple(symbols))
        if self.failures:
            raise self.failures.pop(0)
        return {
            symbol: Quote(symbol=symbol, price=100.0, change_pct=1.0, volume=1000.0, ts=NOW)
            for symbol in symbols
        }


def coordinator(provider, **overrides):
    kwargs = dict(
        feed="iex",
        asset_cache_ttl=timedelta(hours=1),
        batch_size=2,
        request_budget=3,
        max_retries=5,
        retry_base_delay=0,
        retry_max_delay=0,
        sleeper=lambda _delay: _no_sleep(),
        random_fn=lambda: 0,
        clock=lambda: NOW,
    )
    kwargs.update(overrides)
    return DiscoveryCoordinator(provider, **kwargs)


async def _no_sleep():
    return None


@pytest.mark.asyncio
async def test_retry_attempts_consume_total_provider_call_budget():
    provider = RetryProvider([ProviderError(503), ProviderError(503), ProviderError(503)])

    result = await coordinator(provider, request_budget=2).acquire()

    assert len(provider.quote_calls) == 2
    assert result.telemetry.snapshot_batches == 1
    assert result.telemetry.snapshot_requests == 2
    assert result.telemetry.snapshot_retries == 1
    assert result.telemetry.failed_batches == 1
    assert result.telemetry.budget_exhausted is True


@pytest.mark.asyncio
async def test_retry_can_recover_only_when_total_call_budget_remains():
    provider = RetryProvider([ProviderError(429, retry_after=0)])

    result = await coordinator(provider, request_budget=2).acquire()

    assert provider.quote_calls == [("AAA", "BBB"), ("AAA", "BBB")]
    assert set(result.quotes) == {"AAA", "BBB"}
    assert result.telemetry.snapshot_batches == 1
    assert result.telemetry.snapshot_requests == 2
    assert result.telemetry.snapshot_retries == 1
    assert result.telemetry.rate_limit_retries == 1
    assert result.telemetry.budget_exhausted is True


@pytest.mark.asyncio
async def test_non_retryable_failure_consumes_one_call_then_next_batch_may_use_budget():
    provider = RetryProvider([ProviderError(400)])

    result = await coordinator(provider, request_budget=2).acquire()

    assert provider.quote_calls == [("AAA", "BBB"), ("CCC", "DDD")]
    assert set(result.quotes) == {"CCC", "DDD"}
    assert result.telemetry.snapshot_batches == 2
    assert result.telemetry.snapshot_requests == 2
    assert result.telemetry.snapshot_retries == 0
    assert result.telemetry.failed_batches == 1
