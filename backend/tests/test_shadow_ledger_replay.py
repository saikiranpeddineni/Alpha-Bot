import pytest
from datetime import datetime, timedelta, UTC, timezone
from unittest.mock import AsyncMock, MagicMock
import pandas as pd
import numpy as np

from app.core.shadow_ledger import ShadowLedger
from app.db.database import Database
from app.core.domain import utcnow
from app.core.engine import TradingEngine

@pytest.mark.asyncio
async def test_shadow_ledger_replay(tmp_path):
    # Set up temporary database path
    db_path = tmp_path / "test_shadow_ledger.db"
    db = Database(db_path)
    
    # 1. Verify schema migration ran successfully on Database initialization
    with db.engine.connect() as conn:
        cursor = conn.exec_driver_sql("PRAGMA table_info(shadow_ledger)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "replayed" in cols
        assert "replay_timestamp" in cols
        assert "simulated_pnl" in cols
        assert "mfe_pct" in cols
        assert "mae_pct" in cols
        assert "exit_reason" in cols
        assert "tp_hit" in cols
        assert "sl_hit" in cols
        assert "rejection_reason" in cols
        assert "rejection_stage" in cols
        assert "final_decision" in cols

    # 2. Record a candidate signal that is rejected (ML gate rejection)
    ts = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    candidate_id = "TEST_CANDIDATE_1"
    
    payload = {
        "candidate_id": candidate_id,
        "timestamp": ts.isoformat(),
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "long",
        "signal_price": 100.0,
        "entry_price": 100.0,
        "stop_loss": 98.0,      # 2% stop loss
        "take_profit": 104.0,   # 4% take profit
        "ml_confidence": 0.45,
        "final_decision": "rejected",
        "rejection_stage": "ml_gate",
        "rejection_reason": "ML confidence below threshold",
        "strategy_params": {
            "trailing_stop_atr_mult": 0.0
        },
        "replayed": False,
    }
    
    db.save_shadow_signal(candidate_id, ts, "AAPL", "momentum", "long", "paper", payload)
    
    # Verify it was saved correctly and columns are null/false initially
    saved = db.get_shadow_signal(candidate_id)
    assert saved is not None
    assert saved["replayed"] is False
    assert saved["simulated_pnl"] is None

    # 3. Create simulated DataFrame of price path (120 minutes)
    # Entry price is 100.0 at bar index 15. The price climbs gradually to 104.0 to hit TP.
    bars = []
    start_time = ts - timedelta(minutes=15)
    for m in range(120):
        bar_ts = start_time + timedelta(minutes=m)
        # Price climbs 0.1 per minute starting from 100.0 at minute 15
        price = 100.0
        if m > 15:
            price = 100.0 + (m - 15) * 0.1
        bars.append({
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "volume": 1000,
            "atr14": 1.0,
        })
        
    df = pd.DataFrame(bars, index=pd.date_range(start=start_time, periods=120, freq="1Min"))
    
    # Mock DataManager
    data_manager = MagicMock()
    data_manager.history = AsyncMock(return_value=df)
    
    # Mock TradingEngine setup
    engine = MagicMock()
    engine.db = db
    engine.data = data_manager
    engine.mode = "paper"
    engine._tick_count = 0
    
    # Attach our implementation methods from TradingEngine to the mock engine instance
    engine._replay_pending_signals = TradingEngine._replay_pending_signals.__get__(engine, MagicMock)
    engine._update_shadow_decision = TradingEngine._update_shadow_decision.__get__(engine, MagicMock)
    
    # 4. Trigger replay pending signals
    await engine._replay_pending_signals(datetime(2026, 7, 20, 16, 0, tzinfo=UTC))
    
    # 5. Assert database signal was updated with correct metrics
    updated = db.get_shadow_signal(candidate_id)
    assert updated is not None
    assert updated["replayed"] is True
    assert updated["exit_reason"] == "tp"
    assert updated["tp_hit"] is True
    assert updated["sl_hit"] is False
    assert updated["simulated_pnl"] == 4.0  # (104.0 - 100.0) / 100.0 * 100.0 = 4.0%
    import pytest
    # Fix A7 (Issue #189): time_to_target_mins is now timestamp-derived (not bars_held).
    # Entry bar = index 16 (m=16), TP-hit bar = index 55 (m=55).
    # Elapsed time = 55 - 16 = 39 minutes. The old code returned bars_held=40 (wrong).
    assert updated["replay_60m"]["time_to_target_mins"] == pytest.approx(39.0)
    assert updated["mfe_pct"] >= 4.0
    assert updated["mae_pct"] <= 0.05  # Initial bar dip is open - 0.05
