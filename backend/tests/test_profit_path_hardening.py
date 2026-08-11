"""Regression tests for profit-path execution bugs found in the 003 audit."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.engine_portfolio import TradingEnginePortfolioMixin
from app.core.domain import OrderType
from app.pis.execution import PISExecutionAdapter
from app.pis.models import (
    EvidenceCoverage,
    ExitAction,
    LifecycleState,
    MarketRegime,
    PISDecision,
    PositionObservation,
    ReasonCode,
    ThesisDrift,
    TradeHealth,
)


def _decision(action: ExitAction, target_stop_loss: float | None = 97.5) -> PISDecision:
    return PISDecision(
        timestamp=datetime.now(timezone.utc),
        symbol="AAPL",
        action=action,
        target_stop_loss=target_stop_loss,
        exit_ratio=0.5,
        reason_codes=[ReasonCode.PROTECTION_RATCHET],
        health=TradeHealth(0.8, 0.8, 0.8, 0.8, 0.8),
        thesis=ThesisDrift(0.1, 0.0, 0, False),
        runner_score=0.8,
        confidence=0.9,
        lifecycle_state=LifecycleState.RUNNER,
        evidence_coverage=EvidenceCoverage(20, 1.0, 0.9),
        peak_r=2.5,
        trough_r=-0.1,
        market_regime=MarketRegime.TREND_RUNNER,
    )


def _observation() -> PositionObservation:
    ts = datetime.now(timezone.utc)
    return PositionObservation(
        timestamp=ts,
        symbol="AAPL",
        side=1.0,
        qty=100.0,
        entry_price=100.0,
        current_price=105.0,
        high=106.0,
        low=104.0,
        close=105.0,
        volume=1000.0,
        atr=2.0,
        initial_stop=95.0,
        risk_per_share=5.0,
        current_stop=97.0,
        take_profit=110.0,
        r_multiple=1.0,
    )


def test_pis_protection_uses_canonical_target_stop_loss() -> None:
    decision = _decision(ExitAction.UPDATE_PROTECTION, 97.5)
    order = PISExecutionAdapter.build_order_request(decision, _observation())

    assert order is not None
    assert order.order_type == OrderType.STOP
    assert order.stop_price == 97.5
    # Legacy engine code must also remain compatible with the canonical field.
    assert decision.target_stop == decision.target_stop_loss == 97.5


def test_reconcile_positions_is_not_a_dropped_coroutine() -> None:
    """The live engine invokes reconciliation without await; it must execute immediately."""
    assert not inspect.iscoroutinefunction(TradingEnginePortfolioMixin._reconcile_positions)

    class FakePortfolio:
        def __init__(self) -> None:
            self._lots = {
                "AAPL": SimpleNamespace(qty=100.0, avg_price=100.0, strategy="momentum")
            }

    class FakeDB:
        def get_latest_entry_order_strategy(self, symbol, mode):
            raise AssertionError("DB lookup should not be needed for an already-matching lot")

    engine = SimpleNamespace(
        portfolio=FakePortfolio(),
        broker=SimpleNamespace(),
        db=FakeDB(),
        mode="paper",
        _position_take_profits={"AAPL": 110.0},
        _position_peaks={"AAPL": 105.0},
        _position_troughs={"AAPL": 99.0},
        reconciliation_failed=True,
    )
    position = SimpleNamespace(symbol="AAPL", qty=100.0, avg_entry_price=100.0, strategy=None)

    TradingEnginePortfolioMixin._reconcile_positions(engine, [position])

    assert engine.reconciliation_failed is False
    assert position.strategy == "momentum"
    assert engine.portfolio._lots["AAPL"].strategy == "momentum"
