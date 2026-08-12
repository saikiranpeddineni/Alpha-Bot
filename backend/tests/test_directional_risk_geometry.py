from __future__ import annotations

import pytest

from app.core.domain import Signal, SignalAction
from app.indicators.ta import enrich
from app.risk.manager import AccountContext, RiskManager
from tests.conftest import MID_SESSION, make_bars


def _ctx() -> AccountContext:
    return AccountContext(
        equity=100_000.0,
        cash=100_000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100_000.0,
        peak_equity=100_000.0,
        day_trades_today=0,
        is_paper=True,
    )


def _signal(action: SignalAction, *, price: float, stop: float | None, target: float | None) -> Signal:
    return Signal(
        symbol="AAPL",
        action=action,
        confidence=0.8,
        strategy="geometry-test",
        reason="risk-boundary regression",
        price=price,
        stop_loss=stop,
        take_profit=target,
    )


@pytest.fixture
def bars():
    return enrich(make_bars(120, price=100.0))


@pytest.fixture
def rm(risk_config, open_clock) -> RiskManager:
    return RiskManager(risk_config, open_clock)


@pytest.mark.parametrize("stop", [100.0, 101.0])
def test_long_rejects_stop_at_or_above_entry(rm: RiskManager, bars, stop: float):
    decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_LONG, price=100.0, stop=stop, target=110.0),
        _ctx(), bars, now=MID_SESSION,
    )
    assert not decision.approved
    assert "stop" in decision.reason.lower()


@pytest.mark.parametrize("target", [100.0, 99.0])
def test_long_rejects_target_at_or_below_entry(rm: RiskManager, bars, target: float):
    decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_LONG, price=100.0, stop=95.0, target=target),
        _ctx(), bars, now=MID_SESSION,
    )
    assert not decision.approved
    assert "target" in decision.reason.lower()


@pytest.mark.parametrize("stop", [100.0, 99.0])
def test_short_rejects_stop_at_or_below_entry(rm: RiskManager, bars, stop: float):
    decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_SHORT, price=100.0, stop=stop, target=90.0),
        _ctx(), bars, now=MID_SESSION,
    )
    assert not decision.approved
    assert "stop" in decision.reason.lower()


@pytest.mark.parametrize("target", [100.0, 101.0])
def test_short_rejects_target_at_or_above_entry(rm: RiskManager, bars, target: float):
    decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_SHORT, price=100.0, stop=105.0, target=target),
        _ctx(), bars, now=MID_SESSION,
    )
    assert not decision.approved
    assert "target" in decision.reason.lower()


def test_valid_long_and_short_geometry_are_approved_symmetrically(rm: RiskManager, bars):
    long_decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_LONG, price=100.0, stop=95.0, target=110.0),
        _ctx(), bars, now=MID_SESSION,
    )
    short_decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_SHORT, price=100.0, stop=105.0, target=90.0),
        _ctx(), bars, now=MID_SESSION,
    )
    assert long_decision.approved, long_decision.reason
    assert short_decision.approved, short_decision.reason
    assert long_decision.order is not None and short_decision.order is not None
    assert long_decision.order.qty == short_decision.order.qty


def test_synthesized_stop_and_target_remain_directional(rm: RiskManager, bars):
    long_decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_LONG, price=100.0, stop=None, target=None),
        _ctx(), bars, now=MID_SESSION,
    )
    short_decision = rm.evaluate_entry(
        _signal(SignalAction.ENTER_SHORT, price=100.0, stop=None, target=None),
        _ctx(), bars, now=MID_SESSION,
    )
    assert long_decision.approved and short_decision.approved
    assert long_decision.order.stop_loss < 100.0
    assert short_decision.order.stop_loss > 100.0
