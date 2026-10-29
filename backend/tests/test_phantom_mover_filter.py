"""Tests for phantom mover and dead-volume filtering in TopMoversScanner and MarketScanner.

Verifies:
1. TopMoversScanner rejects phantom prints (such as AXG +14,309% on 100 shares) when dollar volume < _MIN_DOLLAR_VOLUME.
2. MarketScanner._stage1_prefilter_parallel rejects dead-volume spikes (100 shares during session) via liquidity filter.
3. MarketScanner correctly computes historical dollar volume without reverse-split share inflation.
4. Legitimate high-volume momentum runners pass both filters cleanly.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import numpy as np
import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.scanner import MarketScanner
from app.core.top_movers_scanner import TopMoversScanner, _MIN_DOLLAR_VOLUME


@pytest.mark.asyncio
async def test_top_movers_screener_rejects_phantom_print_with_raw_volume():
    """Verify screener drops tickers whose raw volume yields dollar volume < $500k."""
    screener = SimpleNamespace(
        get_market_movers=MagicMock(
            return_value=SimpleNamespace(
                # AXG scenario: $4.15 on 100 shares = $415 dollar volume
                gainers=[
                    SimpleNamespace(symbol="AXG", price=4.15, percent_change=14309.72, volume=100.0),
                    SimpleNamespace(symbol="REAL", price=10.0, percent_change=15.0, volume=200_000.0),
                ],
                losers=[],
            )
        )
    )
    provider = SimpleNamespace(_screener_client=screener)
    scanner = TopMoversScanner(SimpleNamespace(provider=provider))

    result = await scanner._fetch_via_screener()
    assert "AXG" not in result.gainers
    assert "REAL" in result.gainers


@pytest.mark.asyncio
async def test_top_movers_screener_filters_via_bars_when_volume_not_in_raw():
    """When Alpaca screener does not provide volume in raw payload, verify bars drop phantom mover."""
    screener = SimpleNamespace(
        get_market_movers=MagicMock(
            return_value=SimpleNamespace(
                gainers=[
                    SimpleNamespace(symbol="AXG", price=4.15, percent_change=14309.72),
                    SimpleNamespace(symbol="LIQUID", price=5.0, percent_change=20.0),
                ],
                losers=[],
            )
        )
    )
    provider = SimpleNamespace(_screener_client=screener)

    # AXG has only 151 shares today at $4.15 ($626 DV), prior days were 50,000 shs at $0.028 ($1,400 DV)
    axg_bars = pd.DataFrame({
        "open": [0.028, 0.028, 4.15],
        "high": [0.029, 0.029, 4.15],
        "low": [0.027, 0.027, 4.15],
        "close": [0.028, 0.028, 4.15],
        "volume": [50_000.0, 50_000.0, 151.0],
    })
    # LIQUID has 500,000 shares at $5.00 ($2.5M DV)
    liquid_bars = pd.DataFrame({
        "open": [4.5, 4.8, 5.0],
        "high": [4.6, 4.9, 5.2],
        "low": [4.4, 4.7, 4.9],
        "close": [4.5, 4.8, 5.0],
        "volume": [400_000.0, 450_000.0, 500_000.0],
    })

    async def mock_multi_history(symbols, timeframe, start, end, prefer_cache=True):
        return {"AXG": axg_bars, "LIQUID": liquid_bars}

    data_mgr = SimpleNamespace(
        provider=provider,
        multi_history=AsyncMock(side_effect=mock_multi_history),
    )
    scanner = TopMoversScanner(data_mgr)

    result = await scanner._fetch_via_screener()
    assert "AXG" not in result.gainers
    assert "LIQUID" in result.gainers


@pytest.mark.asyncio
async def test_market_scanner_stage1_rejects_phantom_print_with_session_liquidity_floor():
    """Verify MarketScanner Stage 1 pre-filter rejects AXG with 151 shares during session."""
    dm = MagicMock()
    # 10:45 AM ET (during regular session, 75 minutes after open)
    now_et = datetime(2026, 10, 29, 14, 45, tzinfo=UTC)

    # AXG 15 days of bars: prior days at $0.028, today at $4.15 with only 151 shares
    idx = pd.date_range(end=now_et, periods=15, freq="D", tz="UTC")
    closes = np.array([0.028] * 14 + [4.15])
    opens = np.array([0.028] * 14 + [4.15])
    vols = np.array([50_000.0] * 14 + [151.0])

    axg_df = pd.DataFrame({
        "open": opens, "high": closes + 0.01, "low": closes - 0.01, "close": closes, "volume": vols
    }, index=idx)

    dm.history = AsyncMock(return_value=axg_df)
    dm.provider = SimpleNamespace(__class__=SimpleNamespace(__name__="AlpacaDataProvider"))

    cfg = RiskConfig(min_price=0.50, min_avg_dollar_volume=250.0)
    scanner = MarketScanner(dm, ["SPY"], risk_config=cfg)

    candidates = await scanner._stage1_prefilter_parallel(as_of=now_et, symbols=["AXG"])
    assert len(candidates) == 0
    assert scanner._last_stage1_rejections.get("LIQUIDITY_FILTER", 0) == 1


@pytest.mark.asyncio
async def test_market_scanner_stage2_damps_dead_volume_momentum():
    """Verify Stage 2 scales momentum down when relative volume is dead (rvol < 0.20)."""
    candidates = [
        {
            "symbol": "AXG",
            "mom3": 7804.76,  # +7800% momentum on reverse split
            "rvol": 0.0081,   # dead volume
            "atr_pct": 12.5,
            "gap_pct": 100.0,
        },
        {
            "symbol": "REAL_RUNNER",
            "mom3": 25.0,     # +25% genuine momentum
            "rvol": 2.5,      # 2.5x normal volume
            "atr_pct": 6.0,
            "gap_pct": 8.0,
        },
    ]

    ranked = MarketScanner._stage2_technical_score(candidates)
    # REAL_RUNNER should rank above AXG because AXG's momentum was unconfirmed by volume
    assert ranked[0]["symbol"] == "REAL_RUNNER"
