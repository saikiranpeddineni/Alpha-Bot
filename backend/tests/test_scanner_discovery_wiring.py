from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner import CANDIDATE_POOL, MarketScanner, ScannerOutcome

HISTORICAL_T = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_scanner_replay_stage1_symbols_never_call_current_discovery():
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider), [])
    scanner._discovery = SimpleNamespace(
        acquire=AsyncMock(side_effect=AssertionError("current discovery reached replay"))
    )
    symbols = await scanner._stage1_symbols(HISTORICAL_T)
    assert symbols == CANDIDATE_POOL
    scanner._discovery.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_passes_live_discovery_shortlist_only_to_stage1(monkeypatch):
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider), [])
    scanner._discovery = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(shortlist=("NEW2", "NEW1")))
    )
    scanner.check_spy_volatility = AsyncMock(return_value=True)
    stage1 = AsyncMock(return_value=[])
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", stage1)
    outcome, symbols = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert symbols == []
    stage1.assert_awaited_once_with(None, ["NEW1", "NEW2"])


@pytest.mark.asyncio
async def test_run_passes_full_static_pool_to_stage1_for_historical_as_of(monkeypatch):
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider), [])
    scanner.check_spy_volatility = AsyncMock(return_value=True)
    stage1 = AsyncMock(return_value=[])
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", stage1)
    outcome, symbols = await scanner.run(HISTORICAL_T)
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert symbols == []
    stage1.assert_awaited_once_with(HISTORICAL_T, CANDIDATE_POOL)
