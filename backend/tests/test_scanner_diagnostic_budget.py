from app.config import RiskConfig
from app.config import Settings
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest

from app.core.scanner import MarketScanner


@pytest.mark.asyncio
async def test_stage1_diagnostics_make_zero_extra_provider_calls():
    provider = SimpleNamespace(get_bars=AsyncMock(return_value=pd.DataFrame()))
    as_of = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)

    async def history(symbol, timeframe, start, end, prefer_cache=True):
        frequency = "min" if timeframe == "1Min" else "D"
        idx = pd.date_range(end=as_of, periods=15, freq=frequency, tz="UTC")
        closes = np.full(len(idx), 150.0)
        return pd.DataFrame(
            {
                "open": closes,
                "high": closes + 0.5,
                "low": closes - 0.5,
                "close": closes,
                "volume": np.full(len(idx), 5_000_000.0),
            },
            index=idx,
        )

    history_mock = AsyncMock(side_effect=history)
    scanner = MarketScanner(
        SimpleNamespace(provider=provider, history=history_mock, original_history=history_mock, risk_config=RiskConfig()),
        ["SPY"],
        risk_config=RiskConfig()
    )

    candidates = await scanner._stage1_prefilter_parallel(as_of=as_of, symbols=["AAPL"])

    assert [candidate["symbol"] for candidate in candidates] == ["AAPL"]
    assert history_mock.await_count == 2
    provider.get_bars.assert_not_awaited()
