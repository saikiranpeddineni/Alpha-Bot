"""Regression tests for the PIS broker-execution boundary.

These tests deliberately stay outside PIS core.  They exercise the engine's
close guard, broker-protection hand-off, and fill/order attribution paths.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.brokers.base import BrokerTimeoutError, Fill
from app.config import Settings
from app.core.domain import (
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Regime,
)
from app.core.durable_engine import DurableTradingEngine
from app.core.engine import TradingEngine
from app.db.database import Database
from app.portfolio.tracker import PortfolioTracker


def _make_engine(tmp_path, *, close_result=None, position_qty: float = 100.0):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "pis-execution.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)

    frame = pd.DataFrame(
        {
            "open": [104.0] * 15,
            "high": [106.0] * 15,
            "low": [103.0] * 15,
            "close": [105.0] * 15,
            "volume": [10_000.0] * 15,
            "atr14": [2.0] * 15,
        },
        index=pd.date_range(
            datetime(2026, 8, 25, 13, 46, tzinfo=UTC), periods=15, freq="min"
        ),
    )

    class _Data:
        provider = SimpleNamespace(name="synthetic")

        def frame(self, *_args, **_kwargs):
            return frame

    engine.data = _Data()
    engine.broker = SimpleNamespace(
        close_position=AsyncMock(return_value=close_result),
    )
    engine.risk = SimpleNamespace()
    engine.pipeline = SimpleNamespace()
    engine.portfolio = SimpleNamespace(
        _lots={"AAPL": SimpleNamespace(qty=position_qty, stop_loss=95.0)},
    )
    engine._position_initial_stops["AAPL"] = 95.0
    engine.strategies = []
    engine.publish = MagicMock()
    engine.pis_adapter = SimpleNamespace(
        evaluate_position_exit=AsyncMock(),
        revert_scale_out=MagicMock(),
        handle_position_closed=MagicMock(),
    )
    return engine, db, frame


def _close_order(
    *,
    status: OrderStatus = OrderStatus.SUBMITTED,
    qty: float = 50.0,
    reason: str = "PIS Exit Reason: SCALE_OUT_TARGET",
) -> Order:
    return Order(
        id="pis-close-1",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=qty,
        order_type=OrderType.MARKET,
        status=status,
        strategy="engine",
        reason=reason,
        leg="exit",
    )


def _pis_order(*, qty: float = 50.0) -> OrderRequest:
    return OrderRequest(
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=qty,
        order_type=OrderType.MARKET,
        strategy="PISDiscretionaryExit[RUNNER]",
        reason="PIS Exit Reason: SCALE_OUT_TARGET",
    )


@pytest.mark.asyncio
async def test_partial_pis_close_repairs_residual_broker_stop_before_return(tmp_path, monkeypatch):
    """A successful partial close must not leave its residual runner naked."""
    import app.core.engine_evaluation as evaluation

    monkeypatch.setattr(evaluation, "enrich", lambda df: df)
    monkeypatch.setattr(evaluation, "detect_regime", lambda _df: (Regime.RANGE, {}))

    engine, db, _frame = _make_engine(tmp_path, close_result=_close_order())
    engine._sync_broker_protective_stop = AsyncMock(return_value=True)
    engine.pis_adapter.evaluate_position_exit.return_value = (
        _pis_order(qty=50.0),
        SimpleNamespace(target_stop_loss=101.0),
    )
    position = Position(
        symbol="AAPL",
        qty=100.0,
        avg_entry_price=100.0,
        market_price=105.0,
        strategy="momentum",
    )

    try:
        await engine._evaluate_symbol("AAPL", position, 100_000.0, datetime.now(UTC))
    finally:
        db.close()

    engine._sync_broker_protective_stop.assert_awaited_once_with(
        "AAPL", 101.0, qty=50.0
    )


@pytest.mark.asyncio
async def test_residual_stop_repair_uses_requested_runner_qty(tmp_path):
    """A missing Alpaca stop leg is repaired for the runner, not the old full lot."""
    engine, db, _frame = _make_engine(tmp_path)
    broker = SimpleNamespace(
        replace_exit_leg=AsyncMock(),
        open_orders=AsyncMock(return_value=[]),
        positions=AsyncMock(
            return_value=[
                Position(
                    symbol="AAPL",
                    qty=100.0,
                    avg_entry_price=100.0,
                    market_price=105.0,
                )
            ]
        ),
        submit=AsyncMock(
            return_value=Order(
                id="stop-repair-1",
                symbol="AAPL",
                side=OrderSide.SELL,
                qty=50.0,
                order_type=OrderType.STOP,
                stop_price=101.0,
                status=OrderStatus.SUBMITTED,
            )
        ),
    )
    engine.broker = broker

    try:
        assert await engine._sync_broker_protective_stop("AAPL", 101.0, qty=50.0)
    finally:
        db.close()

    request = broker.submit.await_args.args[0]
    assert request.qty == 50.0
    assert request.stop_price == 101.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_result",
    [None, _close_order(status=OrderStatus.REJECTED), _close_order(status=OrderStatus.CANCELED)],
)
async def test_failed_pis_close_returns_false_releases_latch_and_reverts_scaleout(
    tmp_path, monkeypatch, close_result
):
    """None/rejected PIS closes are failures, not filled scale-outs."""
    import app.core.engine_evaluation as evaluation

    monkeypatch.setattr(evaluation, "enrich", lambda df: df)
    monkeypatch.setattr(evaluation, "detect_regime", lambda _df: (Regime.RANGE, {}))

    engine, db, _frame = _make_engine(tmp_path, close_result=close_result)
    engine.pis_adapter.evaluate_position_exit.return_value = (
        _pis_order(qty=50.0),
        SimpleNamespace(target_stop_loss=101.0),
    )
    position = Position(
        symbol="AAPL",
        qty=100.0,
        avg_entry_price=100.0,
        market_price=105.0,
        strategy="momentum",
    )

    try:
        await engine._evaluate_symbol("AAPL", position, 100_000.0, datetime.now(UTC))
    finally:
        db.close()

    engine.pis_adapter.revert_scale_out.assert_called_once_with("AAPL")
    assert "AAPL" not in engine._pending_close_symbols


@pytest.mark.asyncio
async def test_ambiguous_pis_close_reports_failure_and_reverts_scaleout(tmp_path):
    """A transport ambiguity cannot be mistaken for a successful scale-out."""
    engine, db, _frame = _make_engine(tmp_path)
    engine.broker.close_position = AsyncMock(
        side_effect=BrokerTimeoutError("close result is ambiguous")
    )

    try:
        with pytest.raises(BrokerTimeoutError):
            await engine._close_position_guarded(
                "AAPL",
                reason="PIS Exit Reason: SCALE_OUT_TARGET",
                qty=50.0,
                strategy="PISDiscretionaryExit[RUNNER]",
            )
    finally:
        db.close()

    assert "AAPL" not in engine._pending_close_symbols


@pytest.mark.asyncio
async def test_full_pis_exit_keeps_no_residual_stop_repair_path(tmp_path, monkeypatch):
    """Full exits retain their existing safety behavior and do not add a new stop."""
    import app.core.engine_evaluation as evaluation

    monkeypatch.setattr(evaluation, "enrich", lambda df: df)
    monkeypatch.setattr(evaluation, "detect_regime", lambda _df: (Regime.RANGE, {}))

    engine, db, _frame = _make_engine(
        tmp_path,
        close_result=_close_order(qty=100.0, reason="PIS Exit Reason: PROTECTION_BREACH"),
        position_qty=100.0,
    )
    engine._sync_broker_protective_stop = AsyncMock(return_value=True)
    engine.pis_adapter.evaluate_position_exit.return_value = (
        _pis_order(qty=100.0).model_copy(
            update={
                "strategy": "PISDiscretionaryExit[RUNNER]",
                "reason": "PIS Exit Reason: PROTECTION_BREACH",
            }
        ),
        SimpleNamespace(target_stop_loss=101.0),
    )
    position = Position(
        symbol="AAPL",
        qty=100.0,
        avg_entry_price=100.0,
        market_price=105.0,
        strategy="momentum",
    )

    try:
        await engine._evaluate_symbol("AAPL", position, 100_000.0, datetime.now(UTC))
    finally:
        db.close()

    engine._sync_broker_protective_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_pis_order_reason_survives_restart_style_fill_and_closes_runner(
    tmp_path,
):
    """A persisted PIS close order supplies the final runner exit reason."""
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "attribution.db"),
        DEFAULT_WATCHLIST="SPY",
    )
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)
    now = datetime(2026, 8, 25, 14, 5, tzinfo=UTC)
    portfolio = PortfolioTracker()
    portfolio.on_fill(
        Fill(
            order_id="entry-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=100.0,
            price=100.0,
            strategy="momentum",
            ts=now - timedelta(minutes=10),
        ),
        initial_stop=95.0,
    )
    portfolio.on_fill(
        Fill(
            order_id="pis-partial-1",
            symbol="AAPL",
            side=OrderSide.SELL,
            qty=50.0,
            price=105.0,
            strategy="PISDiscretionaryExit[RUNNER]",
            reason="PIS Exit Reason: SCALE_OUT_TARGET",
            ts=now - timedelta(minutes=5),
        )
    )

    final_reason = "PIS Exit Reason: PROTECTION_BREACH"
    persisted_close = Order(
        id="pis-final-1",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=50.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.SUBMITTED,
        strategy="PISDiscretionaryExit[RUNNER]",
        reason=final_reason,
        leg="exit",
    )
    engine.recent_orders.append(persisted_close)
    engine.portfolio = portfolio
    engine.broker = SimpleNamespace(
        drain_fills=lambda: [
            Fill(
                order_id="pis-final-1",
                symbol="AAPL",
                side=OrderSide.SELL,
                qty=50.0,
                price=110.0,
                strategy="alpaca",
                reason="",
                ts=now,
            )
        ]
    )
    engine.risk = SimpleNamespace(
        on_trade_closed=MagicMock(),
        get_risk_state=MagicMock(return_value={}),
    )
    engine.perf_tracker = MagicMock()
    engine.publish = MagicMock()

    try:
        await engine._drain_fills(now)
    finally:
        db.close()

    assert portfolio.trades
    assert portfolio.trades[-1].strategy == "momentum"
    assert portfolio.trades[-1].exit_reason == final_reason
    assert not portfolio.trades[-1].exit_reason.startswith("unspecified_exit")


def test_durable_fill_observation_preserves_pis_reason_and_strategy():
    """Durable order updates must not erase known PIS fill attribution."""
    existing = Order(
        id="pis-final-2",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=10.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.SUBMITTED,
        strategy="engine",
        reason="",
    )
    fill = SimpleNamespace(
        order_id="pis-final-2",
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=10.0,
        price=110.0,
        strategy="PISDiscretionaryExit[RUNNER]",
        reason="PIS Exit Reason: PROTECTION_BREACH",
        ts=datetime(2026, 8, 25, 14, 5, tzinfo=UTC),
        cumulative_qty=10.0,
    )
    saved = []
    engine = object.__new__(DurableTradingEngine)
    engine.mode = "demo"
    engine.recent_orders = deque([existing], maxlen=200)
    engine.db = SimpleNamespace(
        get_order=lambda _order_id: None,
        save_order=lambda order, mode: saved.append((order, mode)),
    )

    engine._persist_observed_fill_order(fill)

    assert existing.reason == fill.reason
    assert existing.strategy == fill.strategy
    assert saved[-1][0] is existing
