import pytest
import pandas as pd
import numpy as np
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

from app.core.scanner import MarketScanner, DEFENSIVE_WATCHLIST, ScannerOutcome, SpyVolatilityState
from app.risk.market_regime import MarketRiskState
from app.indicators.ta import enrich


@pytest.fixture
def mock_data_manager():
    dm = MagicMock()
    dm.history = AsyncMock()
    dm.original_history = dm.history
    dm.provider.get_bars = AsyncMock(return_value=pd.DataFrame())
    return dm


@pytest.mark.anyio
async def test_spy_volatility_checks(mock_data_manager):
    scanner = MarketScanner(mock_data_manager, ["SPY", "QQQ"])
    
    # Mock VIX history returning low volatility (hourly bars)
    idx = pd.date_range(end=datetime.now(UTC), periods=300, freq="1h", tz="UTC")
    closes = np.ones(300) * 15.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.01, "low": closes - 0.01, "close": closes, "volume": 1000.0
    }, index=idx)
    mock_data_manager.history.return_value = df
    
    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.NORMAL, 1.0, 1.0, [], {}))
    res = await scanner.check_spy_volatility()
    assert res is SpyVolatilityState.SAFE  # NORMAL/CAUTION map to legacy SAFE


@pytest.mark.anyio
async def test_pool_screener_filtering(mock_data_manager):
    from app.config import RiskConfig
    test_risk = RiskConfig(
        min_price=10.0,
        min_avg_dollar_volume=1_000_000.0,
    )
    scanner = MarketScanner(mock_data_manager, ["SPY", "QQQ"], risk_config=test_risk)
    
    async def mock_history(symbol, timeframe, start, end, prefer_cache=True):
        idx = pd.date_range(end=datetime.now(UTC), periods=15, freq="D", tz="UTC")
        if symbol == "AAPL":
            closes = np.ones(15) * 150.0
            vols = np.ones(15) * 5_000_000.0
        elif symbol == "MSFT":
            # Fails: Price $8 (below $10 minimum price floor)
            closes = np.ones(15) * 8.0
            vols = np.ones(15) * 5_000_000.0
        else:
            # Fails: Vol 500k
            closes = np.ones(15) * 100.0
            vols = np.ones(15) * 500_000.0
            
        return pd.DataFrame({
            "open": closes, "high": closes + 0.5, "low": closes - 0.5, "close": closes, "volume": vols
        }, index=idx)
        
    mock_data_manager.history.side_effect = mock_history
    
    from app.core import scanner as scanner_module
    orig_pool = scanner_module.CANDIDATE_POOL
    scanner_module.CANDIDATE_POOL = ["AAPL", "MSFT", "TSLA"]
    
    try:
        candidates = await scanner.screen_pool()
        assert len(candidates) == 1
        assert candidates[0]["symbol"] == "AAPL"
        assert candidates[0]["price"] == 150.0
        assert candidates[0]["avg_vol"] == 5_000_000.0
    finally:
        scanner_module.CANDIDATE_POOL = orig_pool


@pytest.mark.anyio
async def test_scanner_panic_fallback(mock_data_manager):
    scanner = MarketScanner(mock_data_manager, ["SPY", "QQQ"])
    
    # First 280 bars: low volatility. Last 20 bars: high volatility spike.
    idx = pd.date_range(end=datetime.now(UTC), periods=300, freq="1h", tz="UTC")
    closes = 100.0 + np.arange(300) * 0.1
    highs = closes.copy()
    lows = closes.copy()
    
    highs[:280] += 0.1
    lows[:280] -= 0.1
    
    highs[280:] += 20.0
    lows[280:] -= 20.0
    
    df = pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes, "volume": 1000.0
    }, index=idx)
    mock_data_manager.history.return_value = df
    
    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.PANIC, 1.0, 1.0, ["test_panic"], {}))
    res = await scanner.check_spy_volatility()
    assert res is SpyVolatilityState.PANIC  # STRESS/PANIC map to legacy PANIC
    
    outcome, watchlist = await scanner.run()
    assert outcome == ScannerOutcome.SUCCESS_NONEMPTY
    assert watchlist == DEFENSIVE_WATCHLIST
