from app.config import RiskConfig
from app.config import Settings
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.scanner import CANDIDATE_POOL, MarketScanner, ScannerOutcome
from app.risk.market_regime import MarketRiskState


@pytest.mark.asyncio
async def test_live_stage1_prefilter_receives_keyword_discovery_contract(monkeypatch):
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider, risk_config=RiskConfig()), [])
    scanner._discovery = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(shortlist=("NEW2", "NEW1")))
    )
    scanner._assess_market_risk = AsyncMock(return_value=(MarketRiskState.NORMAL, 0.85, []))
    stage1 = AsyncMock(return_value=[])
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", stage1)

    outcome, symbols = await scanner.run()

    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert symbols == []
    stage1.assert_awaited_once_with(as_of=None, symbols=["NEW1", "NEW2"])


@pytest.mark.asyncio
async def test_replay_stage1_prefilter_receives_keyword_static_contract(monkeypatch):
    historical_t = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
    provider = SimpleNamespace(name="alpaca", supports_asset_discovery=True)
    scanner = MarketScanner(SimpleNamespace(provider=provider, risk_config=RiskConfig()), [])
    scanner._assess_market_risk = AsyncMock(return_value=(MarketRiskState.NORMAL, 0.85, []))
    stage1 = AsyncMock(return_value=[])
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", stage1)

    outcome, symbols = await scanner.run(historical_t)

    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert symbols == []
    stage1.assert_awaited_once_with(as_of=historical_t, symbols=CANDIDATE_POOL)
