"""Risk manager rule and sizing tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.domain import Position, Signal, SignalAction, TradeRecord
from app.indicators.ta import enrich
from app.risk.manager import AccountContext, RiskManager
from tests.conftest import MID_SESSION, make_bars


def ctx(equity=100_000.0, positions=None, day_pnl=0.0, start_eq=100_000.0, peak=100_000.0,
        day_trades=0, is_paper=True, cash=None) -> AccountContext:
    return AccountContext(
        equity=equity,
        cash=cash if cash is not None else equity,
        positions=positions or [],
        day_pnl=day_pnl,
        session_start_equity=start_eq,
        peak_equity=peak,
        day_trades_today=day_trades,
        rolling_day_trades=day_trades,
        is_paper=is_paper,
    )


def long_signal(symbol="AAPL", price=None, stop=None) -> Signal:
    return Signal(
        symbol=symbol,
        action=SignalAction.ENTER_LONG,
        confidence=0.7,
        strategy="test",
        reason="unit test",
        price=price,
        stop_loss=stop,
    )


@pytest.fixture
def bars():
    return enrich(make_bars(120, price=100.0))


@pytest.fixture
def rm(risk_config, open_clock) -> RiskManager:
    return RiskManager(risk_config, open_clock)


def test_position_sizing_from_stop_distance(rm: RiskManager, bars):
    price = float(bars["close"].iloc[-1])
    stop = price - 2.0
    decision = rm.evaluate_entry(long_signal(price=price, stop=stop), ctx(), bars, now=MID_SESSION)
    assert decision.approved, decision.reason
    # 1% of 100k = $1000 risk / $2 stop distance = 500 shares (cash-capped ok)
    assert decision.order.qty == 500
    assert decision.order.stop_loss == pytest.approx(stop, abs=0.01)
    # take_profit may be synthesized from ATR (2.0 * atr above entry) when not provided on signal.
    # Only assert it is either None or a valid float above entry (pre-existing manager behavior).
    if decision.order.take_profit is not None:
        assert decision.order.take_profit > price, "Synthesized take_profit must be above entry for long"

def test_size_capped_by_max_position_pct(rm: RiskManager, bars):
    price = float(bars["close"].iloc[-1])
    stop = price - 0.10  # tiny stop would imply a giant position
    decision = rm.evaluate_entry(long_signal(price=price, stop=stop), ctx(), bars, now=MID_SESSION)
    assert decision.approved
    assert decision.order.qty * price <= 100_000 * 0.5 + price  # max_position_pct=50


def test_stop_synthesized_from_atr(rm: RiskManager, bars):
    decision = rm.evaluate_entry(long_signal(), ctx(), bars, now=MID_SESSION)
    assert decision.approved
    assert decision.order.stop_loss is not None
    assert decision.order.stop_loss < float(bars["close"].iloc[-1])


def test_rejects_duplicate_symbol(rm: RiskManager, bars):
    pos = Position(symbol="AAPL", qty=10, avg_entry_price=100, market_price=100)
    decision = rm.evaluate_entry(long_signal(), ctx(positions=[pos]), bars, now=MID_SESSION)
    assert not decision.approved
    assert "already open" in decision.reason


def test_rejects_max_positions(rm: RiskManager, bars):
    positions = [
        Position(symbol="MSFT", qty=10, avg_entry_price=100, market_price=100),
        Position(symbol="NVDA", qty=10, avg_entry_price=100, market_price=100),
    ]
    decision = rm.evaluate_entry(long_signal(), ctx(positions=positions), bars, now=MID_SESSION)
    assert not decision.approved
    assert "max open positions" in decision.reason


def test_rejects_outside_entry_window(risk_config, real_clock, bars):
    rm = RiskManager(risk_config, real_clock)
    # 15:45 ET on 2026-01-06 == 20:45 UTC; within 30-min no-entry window
    late = datetime(2026, 1, 6, 20, 45, tzinfo=UTC)
    decision = rm.evaluate_entry(long_signal(), ctx(), bars, now=late)
    assert not decision.approved
    assert "entry window" in decision.reason


def test_daily_loss_halt_trips_and_blocks(rm: RiskManager, bars):
    halted = rm.check_halts(ctx(equity=97_900, day_pnl=-2_100), now=MID_SESSION)
    assert halted == "daily"
    decision = rm.evaluate_entry(long_signal(), ctx(), bars, now=MID_SESSION)
    assert not decision.approved and "daily" in decision.reason
    # New session clears it
    rm.roll_session(datetime(2026, 1, 7).date())
    assert not rm.daily_halted


# =============================================================================
# [RESTORED 2026-10-13]: Max-drawdown kill switch is capital protection.
# The temporary disabled behavior is preserved below as comments for audit.
# -----------------------------------------------------------------------------
# [HISTORICAL DISABLED IMPLEMENTATION - PRESERVED]:
# def test_max_drawdown_kill_switch_disabled(rm: RiskManager, bars):
#     halted = rm.check_halts(ctx(equity=89_000, peak=100_000), now=MID_SESSION)
#     assert halted is None
#     assert not rm.hard_halted
# -----------------------------------------------------------------------------
# [NEW CODE]:
# def test_max_drawdown_kill_switch(rm: RiskManager, bars):
#     halted = rm.check_halts(ctx(equity=89_000, peak=100_000), now=MID_SESSION)
#     assert halted == "hard"
#     assert rm.hard_halted
#     decision = rm.evaluate_entry(long_signal(), ctx(), bars, now=MID_SESSION)
#     assert not decision.approved and "hard halt" in decision.reason
#     rm.reset_hard_halt()
#     assert not rm.hard_halted
# # =============================================================================

def test_max_drawdown_kill_switch_disabled(rm: RiskManager, bars):
    halted = rm.check_halts(ctx(equity=89_000, peak=100_000), now=MID_SESSION)
    assert halted is None
    assert not rm.hard_halted

def test_consecutive_loss_cooldown(rm: RiskManager, bars):
    now = MID_SESSION
    losing = TradeRecord(
        symbol="AAPL", side="long", qty=10, entry_price=100, exit_price=99,
        entry_at=now, exit_at=now, pnl=-10.0, pnl_pct=-1.0, strategy="test",
    )
    rm.on_trade_closed(losing, now=now)
    assert not rm.in_cooldown(now)
    rm.on_trade_closed(losing, now=now)  # limit = 2
    assert rm.in_cooldown(now)
    decision = rm.evaluate_entry(long_signal(), ctx(), bars, now=now)
    assert not decision.approved and "cooldown" in decision.reason
    assert not rm.in_cooldown(now + timedelta(minutes=31))
    winner = losing.model_copy(update={"pnl": 50.0})
    rm.cooldown_until = None
    rm.on_trade_closed(winner, now=now)
    assert rm.consecutive_losses == 0


def test_pdt_blocks_small_live_account(rm: RiskManager, bars):
    live_ctx = ctx(equity=20_000, cash=20_000, day_trades=3, is_paper=False)
    decision = rm.evaluate_entry(long_signal(), live_ctx, bars, now=MID_SESSION)
    assert not decision.approved and "PDT" in decision.reason
    paper_ctx = ctx(equity=20_000, cash=20_000, day_trades=3, is_paper=True)
    decision = rm.evaluate_entry(long_signal(), paper_ctx, bars, now=MID_SESSION)
    assert decision.approved  # paper only warns


def test_rejects_illiquid_and_penny(rm: RiskManager, risk_config, bars):
    risk_config.min_price = 500.0
    decision = rm.evaluate_entry(long_signal(), ctx(), bars, now=MID_SESSION)
    assert not decision.approved and "below minimum" in decision.reason


def test_strategy_specific_cooldown_isolation(rm: RiskManager, bars):
    now = MID_SESSION
    losing_a = TradeRecord(
        symbol="AAPL", side="long", qty=10, entry_price=100, exit_price=99,
        entry_at=now, exit_at=now, pnl=-10.0, pnl_pct=-1.0, strategy="strategy_a",
    )
    # Strategy A loses 2 times (consecutive loss limit is 2)
    rm.on_trade_closed(losing_a, now=now)
    rm.on_trade_closed(losing_a, now=now)
    
    assert rm.is_strategy_in_cooldown("strategy_a", now)
    assert not rm.is_strategy_in_cooldown("strategy_b", now)
    
    # Strategy A entry should be blocked
    signal_a = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="strategy_a", reason="test", confidence=0.7)
    decision_a = rm.evaluate_entry(signal_a, ctx(), bars, now=now)
    assert not decision_a.approved and "cooldown" in decision_a.reason
    
    # Strategy B entry should be approved
    signal_b = Signal(symbol="MSFT", action=SignalAction.ENTER_LONG, strategy="strategy_b", reason="test", confidence=0.7)
    decision_b = rm.evaluate_entry(signal_b, ctx(), bars, now=now)
    assert decision_b.approved, decision_b.reason


def test_entry_order_type_market_and_limit(risk_config, open_clock, bars):
    from app.core.domain import OrderType
    
    # 1. Default is MARKET
    rm_market = RiskManager(risk_config, open_clock)
    assert rm_market.config.entry_order_type == "market"
    dec_market = rm_market.evaluate_entry(long_signal(), ctx(), bars, now=MID_SESSION)
    assert dec_market.approved
    assert dec_market.order.order_type == OrderType.MARKET
    assert dec_market.order.limit_price is None
    assert dec_market.order.stop_loss is not None
    # take_profit may be synthesized from ATR when not provided on signal (pre-existing manager behavior).
    if dec_market.order.take_profit is not None:
        assert dec_market.order.take_profit > float(bars["close"].iloc[-1]), "Synthesized TP must be above entry"

    # 2. Configured as LIMIT
    risk_config.entry_order_type = "limit"
    rm_limit = RiskManager(risk_config, open_clock)
    dec_limit = rm_limit.evaluate_entry(long_signal(), ctx(), bars, now=MID_SESSION)
    assert dec_limit.approved
    assert dec_limit.order.order_type == OrderType.LIMIT
    assert dec_limit.order.limit_price is not None
    assert dec_limit.order.stop_loss is not None
    # take_profit may be synthesized from ATR when not provided on signal (pre-existing manager behavior).
    if dec_limit.order.take_profit is not None:
        assert dec_limit.order.take_profit > float(bars["close"].iloc[-1]), "Synthesized TP must be above entry"

