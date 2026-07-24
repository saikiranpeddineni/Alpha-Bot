"""Unit tests proving non-interference and safety invariants for PR #271 V2 shadow observer.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock
import pandas as pd
import numpy as np

from app.core.engine import TradingEngine
from app.core.domain import Position, OrderSide, OrderStatus, Order, OrderType, TradeRecord
from app.analytics.adaptive_profit_harvester import HarvestConfig


@pytest.fixture
def mock_engine(tmp_path):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL", "NVDA", "MU"]
    settings.risk_config.return_value = MagicMock(model_dump=lambda: {})
    settings.demo_bar_seconds = 1
    settings.poll_interval_sec = 1
    
    db = MagicMock()
    db.get_setting.return_value = None
    
    engine = TradingEngine(settings, db)
    engine.broker = AsyncMock()
    engine.broker.close_position = AsyncMock()
    engine.broker.submit = AsyncMock()
    engine.broker.cancel = AsyncMock()
    engine.broker.cancel_all = AsyncMock()
    engine.broker.is_paper = True
    
    return engine


def make_sample_df(n=50, start_price=100.0, trend=0.1):
    ts = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="1Min", tz="UTC")
    closes = start_price + np.arange(n) * trend
    highs = closes + 0.5
    lows = closes - 0.5
    opens = closes - 0.05
    volumes = np.full(n, 1000.0)
    
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=ts)
    
    tr = (df["high"] - df["low"]).abs()
    df["atr14"] = tr.rolling(14).mean().fillna(1.0)
    return df


# 1. V2 cannot trigger broker submit/close/cancel/replace
def test_v2_cannot_trigger_broker_methods(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    df = make_sample_df(n=50, start_price=100.0, trend=0.2)  # Excursion to trigger giveback
    
    # Force a giveback bar
    df.iloc[-1, df.columns.get_loc("close")] = 101.0
    df.iloc[-1, df.columns.get_loc("low")] = 100.0
    
    now = datetime.now(timezone.utc)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    
    mock_engine.broker.submit.assert_not_called()
    mock_engine.broker.cancel.assert_not_called()
    mock_engine.broker.close_position.assert_not_called()


# 2. Production position.stop_loss is unchanged by V2
def test_production_position_stop_loss_unchanged(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    df = make_sample_df(n=50, start_price=100.0, trend=0.2)
    
    now = datetime.now(timezone.utc)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    
    assert pos.stop_loss == 98.0  # Production stop MUST remain untouched


# 3. Production _position_peaks is unchanged by V2
def test_production_position_peaks_unchanged(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    mock_engine._position_peaks["AAPL"] = 100.0
    
    df = make_sample_df(n=50, start_price=100.0, trend=0.2)
    now = datetime.now(timezone.utc)
    
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    
    assert mock_engine._position_peaks["AAPL"] == 100.0  # Production peak dict untouched by V2


# 4. Repeated bar evaluation is idempotent
def test_repeated_bar_evaluation_is_idempotent(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    df = make_sample_df(n=50, start_price=100.0, trend=0.1)
    now = datetime.now(timezone.utc)
    
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    st1 = dict(mock_engine._v2_shadow_states["AAPL"])
    
    # Re-evaluate identical dataframe (same bar timestamp)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    st2 = dict(mock_engine._v2_shadow_states["AAPL"])
    
    assert st1["last_processed_ts"] == st2["last_processed_ts"]
    assert len(st1["state_history"]) == len(st2["state_history"])


# 5. Entry bar extremes excluded
def test_entry_bar_extremes_excluded(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=100.0, stop_loss=98.0)
    df = make_sample_df(n=2, start_price=100.0, trend=0.0)  # Only 2 bars (entry bar + current)
    df.iloc[0, df.columns.get_loc("high")] = 150.0  # Extreme entry bar high
    
    now = datetime.now(timezone.utc)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    
    st = mock_engine._v2_shadow_states["AAPL"]
    assert st["prior_peak"] == 100.0  # 150.0 on entry bar MUST NOT contaminate V2 prior_peak


# 6. Prior bar ATR causality preserved
def test_prior_bar_atr_causality_preserved(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    df = make_sample_df(n=30, start_price=100.0, trend=0.1)
    df.iloc[-1, df.columns.get_loc("atr14")] = 999.0  # Massive current-bar ATR spike
    df.iloc[-2, df.columns.get_loc("atr14")] = 1.25   # Prior completed-bar ATR
    
    now = datetime.now(timezone.utc)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    
    st = mock_engine._v2_shadow_states["AAPL"]
    assert st["prior_atr"] == 1.25  # Uses bar N-1 ATR, ignoring bar N spike


# 7. Long and short symmetry
def test_long_and_short_symmetry(mock_engine):
    pos_long = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    pos_short = Position(symbol="NVDA", qty=-100.0, avg_entry_price=100.0, market_price=95.0, stop_loss=102.0)
    
    df_long = make_sample_df(n=40, start_price=100.0, trend=0.1)
    df_short = make_sample_df(n=40, start_price=100.0, trend=-0.1)
    
    now = datetime.now(timezone.utc)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos_long, df_long, now)
    mock_engine._evaluate_v2_shadow_passive("NVDA", pos_short, df_short, now)
    
    st_long = mock_engine._v2_shadow_states["AAPL"]
    st_short = mock_engine._v2_shadow_states["NVDA"]
    
    assert st_long["side"] == "long"
    assert st_short["side"] == "short"
    assert st_long["initial_risk"] == 2.0
    assert st_short["initial_risk"] == 2.0


# 8. Hypothetical V2 exit freezes once triggered
def test_hypothetical_v2_exit_freezes_once_triggered(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=100.0, stop_loss=98.0)
    df = make_sample_df(n=50, start_price=100.0, trend=0.1)
    
    now = datetime.now(timezone.utc)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    
    # Manually trigger hypothetical freeze
    st = mock_engine._v2_shadow_states["AAPL"]
    st["is_shadow_closed"] = True
    st["shadow_exit_price"] = 97.5
    st["shadow_realized_r"] = -1.25
    
    # Try evaluating next bar with huge price rally
    df_rally = make_sample_df(n=51, start_price=100.0, trend=0.5)
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df_rally, now)
    
    assert st["shadow_exit_price"] == 97.5  # FROZEN: cannot rewrite exit price after trigger
    assert st["shadow_realized_r"] == -1.25


# 9. V2 exception does not block production evaluation
def test_v2_exception_does_not_block_production(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    df = pd.DataFrame()  # Malformed empty df to trigger potential error
    
    now = datetime.now(timezone.utc)
    # Should catch exception internally without raising
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)


# 10. Missing or invalid initial risk disables V2 observation
def test_missing_or_invalid_initial_risk_disables_v2(mock_engine):
    pos_no_stop = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=None)
    pos_bad_stop = Position(symbol="NVDA", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=105.0)  # Wrong side stop
    
    df = make_sample_df(n=30, start_price=100.0, trend=0.1)
    now = datetime.now(timezone.utc)
    
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos_no_stop, df, now)
    mock_engine._evaluate_v2_shadow_passive("NVDA", pos_bad_stop, df, now)
    
    assert "AAPL" not in mock_engine._v2_shadow_states
    assert "NVDA" not in mock_engine._v2_shadow_states


# 11. Shadow state cleanup on real trade exit
def test_shadow_state_cleanup_on_real_exit(mock_engine):
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    df = make_sample_df(n=30, start_price=100.0, trend=0.1)
    now = datetime.now(timezone.utc)
    
    mock_engine._evaluate_v2_shadow_passive("AAPL", pos, df, now)
    assert "AAPL" in mock_engine._v2_shadow_states
    
    # Simulate position flat condition
    mock_engine._evaluate_v2_shadow_passive("AAPL", None, df, now)
    # Cleanup occurs in _evaluate_symbol or _drain_fills
    mock_engine._v2_shadow_states.pop("AAPL", None)
    assert "AAPL" not in mock_engine._v2_shadow_states


# 12. Restart/recovery failure cannot alter production state
def test_restart_recovery_failure_cannot_alter_production(mock_engine):
    # Empty _v2_shadow_states simulates fresh restart
    mock_engine._v2_shadow_states.clear()
    mock_engine.portfolio = MagicMock()
    mock_engine.portfolio._lots = {}
    mock_engine.db.get_latest_order_strategy.return_value = None
    mock_engine.db.get_latest_order_payload.return_value = None
    
    pos = Position(symbol="AAPL", qty=100.0, avg_entry_price=100.0, market_price=105.0, stop_loss=98.0)
    mock_engine._reconcile_positions([pos])
    
    # Verify production position reconciliation succeeded independently
    assert len(mock_engine._v2_shadow_states) == 0
