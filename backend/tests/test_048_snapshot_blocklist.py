"""Tests for issue 048 — snapshot parse failure deduplication."""
from __future__ import annotations
import pytest
import logging
from unittest.mock import MagicMock, patch


@pytest.fixture
def alpaca_provider():
    """AlpacaDataProvider with mocked SDK to avoid needing real API keys."""
    with patch('alpaca.data.historical.StockHistoricalDataClient'), \
         patch('alpaca.trading.client.TradingClient'):
        from app.data.alpaca_data import AlpacaDataProvider
        provider = AlpacaDataProvider(api_key='test', api_secret='test')
        return provider


@pytest.mark.anyio
async def test_snapshot_warning_suppressed_after_threshold(alpaca_provider, caplog):
    """WARNING logged for first N failures; DEBUG logged for subsequent ones."""
    provider = alpaca_provider

    def make_failing_snapshot():
        snap = MagicMock()
        snap.latest_trade = None  # triggers AttributeError on .price
        return {'DEAD': snap}

    with patch.object(provider._client, 'get_stock_snapshot', side_effect=lambda req: make_failing_snapshot()):
        with caplog.at_level(logging.DEBUG, logger='alphadesk'):
            # Call 1 — should warn
            await provider.get_quotes(['DEAD'])
            # Call 2 — should warn
            await provider.get_quotes(['DEAD'])
            # Call 3 — should be DEBUG (suppressed)
            await provider.get_quotes(['DEAD'])

    warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING and 'DEAD' in r.message]
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG and 'DEAD' in r.message and 'suppressed' in r.message]

    assert len(warn_msgs) == 2, f'Expected 2 warnings, got {len(warn_msgs)}'
    assert len(debug_msgs) >= 1, f'Expected at least 1 suppressed debug, got {len(debug_msgs)}'
    assert provider._snapshot_fail_counts.get('DEAD') == 3


@pytest.mark.anyio
async def test_fail_count_resets_on_success(alpaca_provider):
    """Consecutive fail count resets when symbol parses successfully."""
    from unittest.mock import MagicMock
    from datetime import datetime, UTC
    provider = alpaca_provider

    # Seed with 3 failures
    provider._snapshot_fail_counts['RECOVERING'] = 3

    # Mock a successful snapshot response
    trade = MagicMock()
    trade.price = 100.0
    trade.timestamp = datetime.now(UTC)
    daily = MagicMock()
    daily.volume = 1_000_000.0
    prev = MagicMock()
    prev.close = 99.0
    snap = MagicMock()
    snap.latest_trade = trade
    snap.daily_bar = daily
    snap.previous_daily_bar = prev

    with patch.object(provider._client, 'get_stock_snapshot', return_value={'RECOVERING': snap}):
        quotes = await provider.get_quotes(['RECOVERING'])

    assert 'RECOVERING' in quotes
    assert provider._snapshot_fail_counts.get('RECOVERING', 0) == 0


def test_get_snapshot_fail_summary(alpaca_provider):
    """get_snapshot_fail_summary returns symbols with count > threshold."""
    provider = alpaca_provider
    provider._snapshot_fail_counts = {'AAAD': 5, 'ACGO': 3, 'ALIVE': 1}
    summary = provider.get_snapshot_fail_summary()
    assert 'AAAD' in summary
    assert 'ACGO' in summary
    assert 'ALIVE' not in summary  # only 1 failure, below threshold
