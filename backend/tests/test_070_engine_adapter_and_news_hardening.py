"""Regression tests for PISEngineAdapter rsi2 extraction and AlpacaDataProvider non-blocking news sentiment."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.core.domain import Position
from app.pis.engine_adapter import PISEngineAdapter


@pytest.mark.asyncio
async def test_engine_adapter_extracts_rsi2_without_name_error() -> None:
    """Verify PISEngineAdapter extracts rsi2 from bar_df without NameError and populates observation."""
    adapter = PISEngineAdapter()
    adapter.persist_profit_memory = False

    pos = Position(
        symbol="AAPL",
        qty=100.0,
        avg_entry_price=150.0,
        current_price=152.0,
        market_price=152.0,
        strategy="rsi2_reversion",
    )

    times = [datetime(2026, 10, 16, 14, i, tzinfo=UTC) for i in range(5)]
    df = pd.DataFrame(
        {
            "open": [149.0, 149.5, 150.0, 151.0, 151.5],
            "high": [150.0, 150.5, 151.0, 152.0, 152.5],
            "low": [148.5, 149.0, 149.5, 150.5, 151.0],
            "close": [149.5, 150.0, 151.0, 151.5, 152.0],
            "volume": [1000.0, 1200.0, 1100.0, 1300.0, 1500.0],
            "atr14": [1.5, 1.5, 1.5, 1.5, 1.5],
            "rsi2": [15.0, 30.0, 50.0, 72.0, 78.5],
        },
        index=times,
    )

    order_req, decision = await adapter.evaluate_position_exit(
        position=pos,
        bar_df=df,
        initial_stop=147.0,
        take_profit=155.0,
        current_stop=147.0,
        timestamp=times[-1],
    )

    # Brain was created and observed the position
    brain = adapter.service.get_brain("AAPL")
    assert brain is not None
    assert brain._last_observation is not None
    # Crucial: rsi2 must be populated with the float 78.5, NOT None
    assert brain._last_observation.rsi2 == 78.5
    assert brain._last_observation.strategy == "rsi2_reversion"


@pytest.mark.asyncio
async def test_alpaca_get_news_sentiment_is_non_blocking() -> None:
    """Verify get_news_sentiment returns immediately and does not block the caller for 5 seconds."""
    from app.data.alpaca_data import AlpacaDataProvider

    provider = AlpacaDataProvider.__new__(AlpacaDataProvider)
    provider._client = MagicMock()
    provider._client._api_key = "test_key"
    provider._client._secret_key = "test_secret"
    provider._news_cache = {}
    from app.pis.sentiment import NewsSentimentAnalyzer
    provider._sentiment_analyzer = NewsSentimentAnalyzer()

    # Measure execution duration of get_news_sentiment - should be sub-10ms (non-blocking)
    start_t = asyncio.get_event_loop().time()
    score = await provider.get_news_sentiment("MSFT")
    elapsed = asyncio.get_event_loop().time() - start_t

    assert score == 0.0  # Cold cache returns 0.0 immediately
    assert elapsed < 0.1, f"get_news_sentiment took {elapsed:.3f}s; must be instantaneous (<0.1s)"
