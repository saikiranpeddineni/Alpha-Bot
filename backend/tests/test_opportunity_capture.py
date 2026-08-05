from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import pandas as pd

from app.core.domain import Regime, Signal, SignalAction, utcnow
from app.db.database import Database
from app.core.engine import TradingEngine, EngineState
from app.strategies import SignalPipeline
from tests.conftest import make_bars


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_opportunity.db"
    return Database(db_path)


@pytest.mark.asyncio
async def test_heartbeat_sector_leadership(temp_db: Database):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL", "SPY"]
    settings.risk_config().model_dump.return_value = {}

    engine = TradingEngine(settings, temp_db)

    # Mock DataManager to return custom bar ETF returns
    data_mock = MagicMock()
    # Return 2 bars for any ETF: first bar low close, second bar high close
    bars_win = pd.DataFrame({"close": [100.0, 105.0], "volume": [100, 100], "high": [106.0, 106.0], "low": [99.0, 99.0]}, index=pd.date_range("2026-01-06 14:30", periods=2, freq="1min", tz="UTC"))
    data_mock.frame.return_value = bars_win
    engine.data = data_mock

    now = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)

    # Executing sector check
    engine._run_heartbeat_tasks(now)
    assert engine._last_heartbeat_hour == 15

    # Executing again with same hour index should return early
    engine._run_heartbeat_tasks(now + timedelta(minutes=5))
    assert engine._last_heartbeat_hour == 15




