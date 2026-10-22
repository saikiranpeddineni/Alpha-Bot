from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner import CANDIDATE_POOL, MarketScanner, ScannerOutcome
from app.replay.market_feed import ReplayDataManager
from app.replay.runtime import ReplayRuntime
from app.risk.market_regime import MarketRiskState

HISTORICAL_T = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_scanner_replay_stage1_symbols_never_call_current_discovery():
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider), ["USER1"])
    scanner._discovery = SimpleNamespace(
        acquire=AsyncMock(side_effect=AssertionError("current discovery reached replay"))
    )
    symbols = await scanner._stage1_symbols(HISTORICAL_T)
    assert symbols == sorted([*CANDIDATE_POOL, "USER1"])
    scanner._discovery.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_scanner_stage1_uses_only_symbols_with_causal_bars():
    bars = []
    for symbol in ("AAPL", "NVDA", "SPY", "QQQ", "^VIX"):
        bars.append(
            {
                "symbol": symbol,
                "ts_dt": HISTORICAL_T,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000.0,
            }
        )

    scanner = MarketScanner(ReplayDataManager(bars), [])

    symbols = await scanner._stage1_symbols(HISTORICAL_T)

    assert symbols == ["AAPL", "NVDA"]


def test_fast_replay_dynamic_scanner_expands_input_surface():
    symbols = ReplayRuntime._expand_dynamic_scanner_symbols(["aapl"], enabled=True)

    assert set(CANDIDATE_POOL).issubset(symbols)
    assert "AAPL" in symbols
    assert symbols == sorted(symbols)


@pytest.mark.asyncio
async def test_run_passes_live_discovery_shortlist_only_to_stage1(monkeypatch):
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider), ["USER1"])
    scanner._discovery = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(shortlist=("NEW2", "NEW1")))
    )
    scanner._assess_market_risk = AsyncMock(return_value=(MarketRiskState.NORMAL, 0.85, []))
    stage1 = AsyncMock(return_value=[])
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", stage1)
    outcome, symbols = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert symbols == []
    stage1.assert_awaited_once_with(as_of=None, symbols=["NEW1", "NEW2", "USER1"])


@pytest.mark.asyncio
async def test_run_passes_full_static_pool_to_stage1_for_historical_as_of(monkeypatch):
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider), [])
    scanner._assess_market_risk = AsyncMock(return_value=(MarketRiskState.NORMAL, 0.85, []))
    stage1 = AsyncMock(return_value=[])
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", stage1)
    outcome, symbols = await scanner.run(HISTORICAL_T)
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert symbols == []
    stage1.assert_awaited_once_with(as_of=HISTORICAL_T, symbols=CANDIDATE_POOL)
