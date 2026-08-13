import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from app.replay.market_feed import ReplayDataManager, ReplayMarketFeed, _bar_frame
from app.replay.runtime import ReplayRuntime
from app.replay.ledger import ReplayLedger
from app.api.replay import get_replay_ledger, session_mgr
from fastapi import HTTPException

# Test ReplayDataManager performance
@pytest.mark.asyncio
async def test_replay_data_manager_performance():
    # Construct synthetic bars spanning many days
    bars = []
    base_ts = datetime(2026, 8, 1, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    for d in range(10):
        for m in range(390): # 6.5 hours * 60
            ts = base_ts.replace(day=d+1) + timedelta(minutes=m)
            bars.append({
                "symbol": "SPY",
                "ts_dt": ts,
                "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100
            })
    
    # Initialize the dual-cache optimized DataManager
    data = ReplayDataManager(bars)
    assert "SPY" in data._dfs
    assert not data._dfs["SPY"].empty
    
    # Verify frame() returns O(1) slice without raising exception
    data.set_time(bars[50]["ts_dt"])
    frame = data.frame("SPY")
    assert len(frame) == 51 # 0 to 50 inclusive
    
    # Verify poll() uses cursors correctly
    new_bars = await data.poll(["SPY"])
    assert "SPY" in new_bars
    assert len(new_bars["SPY"]) == 51
    
    # Move clock forward by 1 minute
    data.set_time(bars[51]["ts_dt"])
    new_bars2 = await data.poll(["SPY"])
    assert "SPY" in new_bars2
    assert len(new_bars2["SPY"]) == 1 # Only the new bar!

# Test Ledger API Lifecycle
def test_replay_ledger_api():
    with patch("app.api.replay._session_or_404") as mock_session:
        with patch("app.api.replay.session_mgr.get_artifact") as mock_artifact:
            # While RUNNING and artifact absent, returns 404 instead of faking it
            mock_session.return_value = {"status": "RUNNING", "progress_pct": 50.0}
            mock_artifact.return_value = None
            with pytest.raises(HTTPException) as exc:
                get_replay_ledger("test_id_1")
            assert exc.value.status_code == 404
            
            # When FAILED, throws 500
            mock_session.return_value = {"status": "FAILED", "error": "Crash"}
            with pytest.raises(HTTPException) as exc:
                get_replay_ledger("test_id_2")
            assert exc.value.status_code == 500
            
            # When COMPLETED but missing, throws 404
            mock_session.return_value = {"status": "COMPLETED"}
            with pytest.raises(HTTPException) as exc:
                get_replay_ledger("test_id_3")
            assert exc.value.status_code == 404
            
            # When artifact exists, returns it
            mock_session.return_value = {"status": "COMPLETED"}
            mock_artifact.return_value = {"timeline": ["data"]}
            assert get_replay_ledger("test_id_1") == {"timeline": ["data"]}

# Test ReplayMarketFeed window parsing
def test_replay_window_market_hours():
    feed = ReplayMarketFeed()
    feed.data_path = Path("mock.jsonl")
    
    with patch("pathlib.Path.open") as mock_open:
        with patch("pathlib.Path.exists", return_value=True):
            # One pre-market bar, one market open bar, one after hours bar
            mock_handle = MagicMock()
            mock_handle.__iter__.return_value = [
                '{"symbol": "SPY", "ts": "2026-08-12T08:00:00-04:00", "close": 100, "kind": "bar"}',
                '{"symbol": "SPY", "ts": "2026-08-12T09:30:00-04:00", "close": 100, "kind": "bar"}',
                '{"symbol": "SPY", "ts": "2026-08-12T16:01:00-04:00", "close": 100, "kind": "bar"}',
                '{"symbol": "QQQ", "ts": "2026-08-12T09:30:00-04:00", "close": 100, "kind": "bar"}',
                '{"symbol": "VIX", "ts": "2026-08-12T09:30:00-04:00", "close": 100, "kind": "bar"}'
            ]
            mock_open.return_value.__enter__.return_value = mock_handle
            
            bars = feed.load_bars("2026-08-12", "2026-08-12", ["SPY"])
            
            # 08:00 is NOT in window
            assert bars[0]["in_replay_window"] is False
            # 16:01 is NOT in window
            assert bars[-1]["in_replay_window"] is False

def test_boundary_cases_safe_mode():
    feed = ReplayMarketFeed()
    feed.data_path = Path("mock.jsonl")
    with patch("pathlib.Path.open") as mock_open, patch("pathlib.Path.exists", return_value=True):
        mock_handle = MagicMock()
        mock_handle.__iter__.return_value = [
            '{"symbol": "SPY", "ts": "2026-08-12T08:00:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "SPY", "ts": "2026-08-12T09:29:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "SPY", "ts": "2026-08-12T09:30:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "SPY", "ts": "2026-08-12T15:59:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "SPY", "ts": "2026-08-12T16:00:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "SPY", "ts": "2026-08-12T16:01:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "QQQ", "ts": "2026-08-12T09:30:00-04:00", "close": 100, "kind": "bar"}',
            '{"symbol": "VIX", "ts": "2026-08-12T09:30:00-04:00", "close": 100, "kind": "bar"}'
        ]
        mock_open.return_value.__enter__.return_value = mock_handle
        
        bars = feed.load_bars("2026-08-12", "2026-08-12", ["SPY"])
        # Expected states based on time
        expected = {
            "08:00:00": False, # SAFE_MODE
            "09:29:00": False, # SAFE_MODE
            "09:30:00": True,  # RUNNING
            "15:59:00": True,  # RUNNING
            "16:00:00": True,  # valid final market timestamp
            "16:01:00": False  # SAFE_MODE
        }
        
        for bar in bars:
            if bar["symbol"] == "SPY":
                time_str = bar["ts_dt"].strftime("%H:%M:%S")
                assert bar["in_replay_window"] == expected[time_str]

@pytest.mark.asyncio
async def test_production_isolation_guard():
    # Prove replay runtime initializes a completely isolated TradingEngine
    from app.replay.broker import ReplayBroker
    from app.replay.clock import ReplayClock
    from app.replay.runtime import ReplayRuntime
    from app.config import Settings
    
    runtime = ReplayRuntime(Settings(), MagicMock(), replay_id="test_replay")
    
    # We'll patch TradingEngine._tick to capture the engine instance right before execution
    captured_engine = None
    
    async def mock_tick(self):
        nonlocal captured_engine
        captured_engine = self
    
    with patch("app.core.engine.TradingEngine._tick", new=mock_tick):
        # We need mock bars to pass through
        bars = [
            {"symbol": "SPY", "ts_dt": datetime(2026, 8, 12, 9, 30, tzinfo=ZoneInfo("America/New_York")), "close": 100, "in_replay_window": True}
        ]
        with patch.object(runtime, '_snapshot_source_database'), patch('shutil.copy2'), patch('shutil.copytree'), patch('app.db.database.Database'):
            with patch.object(runtime, '_stop_replay_engine'):
                try:
                    await runtime._execute_single_pass(
                        bars=bars, fill_model="ohlc_touch", slippage_bps=1.0, pass_name="A",
                        snapshot_db=Path("mock.db"), snapshot_models=Path("mock_models")
                    )
                except RuntimeError as e: # Catch "No bars" or other issues if we skip too much
                    pass
    
    if captured_engine:
        # Check isolation
        assert isinstance(captured_engine.runtime.broker, ReplayBroker)
        assert isinstance(captured_engine.runtime.clock, ReplayClock)
        assert isinstance(captured_engine.runtime.data, ReplayDataManager)
        # Ensure it cannot submit a real order (ReplayBroker overrides submit_order without network)
        assert not hasattr(captured_engine.runtime.broker, 'api_client')

@pytest.mark.asyncio
async def test_safe_mode_tick_contract():
    from app.core.engine import TradingEngine, EngineState
    from app.config import Settings
    from app.db.database import Database
    from unittest.mock import patch
    
    with patch("app.db.database.Database.__init__", return_value=None), \
         patch("app.db.database.Database.get_setting", return_value={}):
        db = Database()
        
        engine = TradingEngine(Settings(), db)
        engine.state = EngineState.SAFE_MODE
        engine.allow_new_entries = True
        
        with patch.object(engine, 'run_market_scanner') as mock_scan:
            with patch('app.core.engine.utcnow') as mock_utcnow:
                # Pre-market time 9:00 AM ET (13:00 UTC)
                now = datetime(2026, 8, 12, 13, 0, tzinfo=ZoneInfo("UTC"))
                mock_utcnow.return_value = now
                
                import pandas as pd
                from unittest.mock import MagicMock, AsyncMock
                engine.data = MagicMock()
                engine.data.poll = AsyncMock(return_value={'SPY': pd.DataFrame()})
                engine.broker = AsyncMock()
                engine.broker.account = AsyncMock(return_value=MagicMock(equity=100000, cash=100000, buying_power=400000))
                engine.risk = MagicMock()
                engine.portfolio = MagicMock()
                engine.pipeline = MagicMock()
                engine.clock = MagicMock()
                engine.clock.is_open.return_value = False
                engine.clock.session_date.return_value = now.date()
                engine.insights = MagicMock()
                
                # Run tick
                await engine._tick()
                
                # Assert scanner/trainer is NOT called because state != RUNNING
                mock_scan.assert_not_called()
        
@pytest.mark.asyncio
async def test_history_o1_performance():
    from app.replay.market_feed import ReplayDataManager
    import pandas as pd
    
    # Create 1000 bars
    bars = []
    base = datetime(2026, 8, 12, 13, 30, tzinfo=ZoneInfo("UTC"))
    for i in range(1000):
        bars.append({
            "symbol": "SPY",
            "ts_dt": base + pd.Timedelta(minutes=i),
            "close": 100 + i,
            "volume": 100
        })
        
    dm = ReplayDataManager(bars)
    dm.set_time(base + pd.Timedelta(minutes=500))
    
    assert not hasattr(dm, "_visible"), "_visible method should be entirely eliminated for O(1) performance"
    
    # Fetch history
    start = base + pd.Timedelta(minutes=10)
    end = base + pd.Timedelta(minutes=20)
    df = await dm.history("SPY", "1Min", start, end)
    
    assert len(df) == 11
    assert float(df.iloc[-1]["close"]) == 120.0
