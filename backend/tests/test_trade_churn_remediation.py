"""
Test suite validating trade churn remediation and commission friction defenses:
1. Symbol max daily trades lockout.
2. 30-minute win cooldown consolidation.
3. ORB 0.75R chop defense deactivation.
"""

from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.domain import TradeRecord, Regime, Signal, SignalAction, Position
from app.risk.manager import RiskManager
from app.strategies.base import StrategyContext
from app.strategies.orb import OpeningRangeBreakout

UTC = timezone.utc


def test_symbol_max_daily_trades_lockout(open_clock):
    """Verify that reaching symbol_max_daily_trades locks out the symbol for the session."""
    cfg = RiskConfig(
        symbol_max_daily_trades=2,
        symbol_win_cooldown_minutes=0,  # disable timestamp cooldown to test count gate in isolation
        symbol_cooldown_minutes=0,
    )
    rm = RiskManager(cfg, open_clock)

    now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)

    # Initial check: approved
    sig = Signal(symbol="META", action=SignalAction.ENTER_LONG, strategy="orb", confidence=1.0, reason="test")
    decision = rm.is_entry_permitted(sig, now=now)
    assert decision.approved, f"Expected approved but got: {decision.reason}"

    # Trade 1 closes (win)
    t1 = TradeRecord(
        symbol="META",
        strategy="orb",
        side="long",
        qty=10,
        entry_price=600.0,
        exit_price=605.0,
        entry_at=now - timedelta(minutes=15),
        exit_at=now,
        pnl=50.0,
        pnl_pct=0.83,
        r_multiple=1.0,
    )
    rm.on_trade_closed(t1, now=now)
    assert rm.symbol_trade_counts["META"] == 1

    # Permitted for second trade
    decision2 = rm.is_entry_permitted(sig, now=now)
    assert decision2.approved

    # Trade 2 closes (win)
    t2 = TradeRecord(
        symbol="META",
        strategy="orb",
        side="long",
        qty=10,
        entry_price=605.0,
        exit_price=610.0,
        entry_at=now + timedelta(minutes=5),
        exit_at=now + timedelta(minutes=20),
        pnl=50.0,
        pnl_pct=0.83,
        r_multiple=1.0,
    )
    rm.on_trade_closed(t2, now=now + timedelta(minutes=20))
    assert rm.symbol_trade_counts["META"] == 2

    # Trade 3 attempt: must be BLOCKED by max daily trades
    decision3 = rm.is_entry_permitted(sig, now=now + timedelta(minutes=25))
    assert not decision3.approved
    assert "reached max daily trades" in decision3.reason


def test_symbol_win_cooldown_duration(open_clock):
    """Verify that a winning trade triggers symbol_win_cooldown_minutes."""
    cfg = RiskConfig(symbol_win_cooldown_minutes=30)
    rm = RiskManager(cfg, open_clock)

    now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    t = TradeRecord(
        symbol="AAPL",
        strategy="orb",
        side="long",
        qty=20,
        entry_price=350.0,
        exit_price=352.0,
        entry_at=now - timedelta(minutes=10),
        exit_at=now,
        pnl=40.0,
        pnl_pct=0.57,
        r_multiple=0.8,
    )
    rm.on_trade_closed(t, now=now)

    # 10 minutes later: still in cooldown
    sig = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", confidence=1.0, reason="test")
    decision = rm.is_entry_permitted(sig, now=now + timedelta(minutes=10))
    assert not decision.approved
    assert "ticker-specific cooldown active" in decision.reason

    # 31 minutes later: cooldown expired (assuming daily trades limit not exceeded)
    decision2 = rm.is_entry_permitted(sig, now=now + timedelta(minutes=31))
    assert decision2.approved


def test_orb_does_not_scale_out_at_075_chop():
    """Verify that ORB does not fire a scale-out half signal at 0.75R in RANGE/VOLATILE regimes."""
    strat = OpeningRangeBreakout()
    strat._targets["TSLA"] = 250.0  # 2R target is at 250.0

    # Build dummy bar DataFrame with DatetimeIndex
    times = pd.date_range("2026-11-12 09:30", periods=20, freq="1min", tz="America/New_York")
    df = pd.DataFrame(
        {
            "timestamp": times,
            "open": [200.0] * 20,
            "high": [202.0] * 20,
            "low": [199.0] * 20,
            "close": [201.0] * 20,
            "volume": [1000] * 20,
            "atr14": [2.0] * 20,
        },
        index=times,
    )

    # Position entered at 200.0, stop at 198.0 (risk = 2.0). 0.75R chop target would be 201.50
    pos = Position(
        symbol="TSLA",
        qty=10,
        avg_entry_price=200.0,
        market_price=201.60,
        stop_loss=198.0,
        strategy="orb",
    )

    ctx = StrategyContext(
        now=times[-1],
        regime=Regime.RANGE,  # Range regime!
        position=pos,
        equity=100_000.0,
    )

    # Set close price to 201.60 (which is > 201.50 chop target, but < 250.0 full target)
    df.loc[df.index[-1], "close"] = 201.60

    sig = strat.on_bar(ctx, "TSLA", df)
    # Under old logic, this would fire SCALE_OUT_HALF "Scaled out 50% at 0.75R (Chop Defense)".
    # Under new logic, it should NOT scale out.
    assert sig is None or sig.action != SignalAction.SCALE_OUT_HALF
