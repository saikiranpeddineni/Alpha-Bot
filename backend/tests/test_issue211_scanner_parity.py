import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pytest

from app.core.scanner import MarketScanner, ScannerOutcome, CANDIDATE_POOL


class MockDataManager:
    def __init__(self, df_dict):
        self.df_dict = df_dict
        self.provider = type("MockProvider", (), {"supports_asset_discovery": False})()

    async def history(self, symbol, timeframe, start, end, prefer_cache=True):
        return self.df_dict.get(symbol, pd.DataFrame())


@pytest.mark.asyncio
async def test_scanner_staleness_across_0930_boundary():
    """Verify that crossing 09:30 ET does not reject prior completed session daily bars."""
    ET = ZoneInfo("America/New_York")

    # Create mock 1Day dataframe with latest bar as previous trading day close
    dates = pd.date_range("2026-07-01", periods=10, freq="B", tz="UTC")
    data_frames = {}
    for sym in CANDIDATE_POOL[:10]:
        df = pd.DataFrame(
            {
                "open": np.full(10, 150.0),
                "high": np.full(10, 155.0),
                "low": np.full(10, 148.0),
                "close": np.full(10, 152.0),
                "volume": np.full(10, 1000000.0),
            },
            index=dates,
        )
        data_frames[sym] = df

    data_mgr = MockDataManager(data_frames)
    scanner = MarketScanner(data_mgr, core_watchlist=["AVGO", "ORCL", "SKHY"])

    # Test times: 09:00 ET, 09:29 ET, 09:30 ET, 10:00 ET on a trading day after dates[-1]
    day = dates[-1] + pd.Timedelta(days=1)
    test_times_et = [
        day.replace(hour=9, minute=0, second=0).astimezone(ET),
        day.replace(hour=9, minute=29, second=0).astimezone(ET),
        day.replace(hour=9, minute=30, second=0).astimezone(ET),
        day.replace(hour=10, minute=0, second=0).astimezone(ET),
    ]

    for t_et in test_times_et:
        t_utc = t_et.astimezone(UTC)
        stage1 = await scanner._stage1_prefilter_parallel(as_of=t_utc, symbols=CANDIDATE_POOL[:10])
        rejections = getattr(scanner, "_last_stage1_rejections", {})
        assert len(stage1) > 0, f"Stage 1 returned 0 candidates at {t_et}. Rejections: {rejections}"
        assert rejections.get("STALE_DATA", 0) == 0, f"STALE_DATA rejections found at {t_et}: {rejections}"


@pytest.mark.asyncio
async def test_scanner_outcome_failed_on_systemic_data_issues(monkeypatch):
    """Verify that systemic data missing/staleness returns ScannerOutcome.FAILED."""
    # Data manager with empty data for all symbols
    data_mgr = MockDataManager({})
    scanner = MarketScanner(data_mgr, core_watchlist=["AVGO"])

    async def mock_assess(as_of=None):
        from app.risk.market_regime import MarketRiskState
        return MarketRiskState.NORMAL, 1.0, []

    monkeypatch.setattr(scanner, "_assess_market_risk", mock_assess)

    outcome, symbols = await scanner.run(as_of=datetime.now(UTC))
    assert outcome == ScannerOutcome.FAILED
    assert symbols == []
