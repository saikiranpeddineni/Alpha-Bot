import pytest
from app.config import Settings
from app.core.engine import TradingEngine
from app.db.database import Database

@pytest.fixture
def test_engine(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "telemetry_test.db"),
        DEFAULT_WATCHLIST="AAPL,NVDA,SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)
    return engine

@pytest.mark.asyncio
async def test_heartbeat_telemetry_payload(test_engine):
    engine = test_engine
    engine.watchlist = ["AAPL", "NVDA", "SPY"]
    engine.system_health = "NORMAL"

    payload = engine.telemetry_payload(event_type="heartbeat_tick", pulse_intensity="small")

    assert "seq" in payload
    assert payload["seq"] >= 1
    assert payload["health"] == "NORMAL"
    assert payload["activity"] in ["IDLE", "SCANNING", "EVALUATING", "TRAINING", "REPLAYING", "TRADING"]
    assert "event" in payload
    assert payload["event"]["type"] == "heartbeat_tick"
    assert "timestamps" in payload
    assert "last_bar" in payload["timestamps"]
    assert "stats" in payload
    assert payload["stats"]["watchlist_count"] == 3
    assert payload["stats"]["broker_connected"] is True

@pytest.mark.asyncio
async def test_telemetry_seq_increments(test_engine):
    engine = test_engine
    p1 = engine.telemetry_payload("heartbeat_tick", "small")
    p2 = engine.telemetry_payload("strategy_evaluated", "medium")

    assert p2["seq"] == p1["seq"] + 1
    assert p2["event"]["type"] == "heartbeat_tick" or p2["event"]["pulse"] == "medium"
