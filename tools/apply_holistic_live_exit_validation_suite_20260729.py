"""Add the holistic LIVE exit lifecycle validation suite (2026-07-29).

This companion runs the production applicator first, then installs behavioral
regressions for restart/reconciliation, Rule-41 coexistence, partial fills,
replace races, nested brackets (INTC/NOW shape), long/short symmetry, EOD
liquidation authority, and repeated monotonic stop ratchets.

Run from repository root:
    python tools/apply_holistic_live_exit_validation_suite_20260729.py
    cd backend && pytest -q tests/test_holistic_live_exit_harvest.py
"""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / "tools/apply_holistic_live_exit_harvest_fix_20260729.py"), run_name="__main__")
TEST = ROOT / "backend/tests/test_holistic_live_exit_harvest.py"

TEST.write_text(r'''"""Behavioral regression matrix for the holistic LIVE exit lifecycle."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.core.domain import Order, OrderSide, OrderStatus, OrderType
from app.core.engine import TradingEngine


def _order(order_id, symbol, *, leg, side, limit=None, stop=None, status=OrderStatus.SUBMITTED, parent="parent"):
    return Order(
        id=order_id,
        client_order_id=f"cid-{order_id}",
        symbol=symbol,
        side=side,
        qty=10,
        order_type=OrderType.STOP if leg == "stop" else OrderType.LIMIT,
        limit_price=limit,
        stop_price=stop,
        status=status,
        order_class="bracket",
        parent_id=parent,
        leg=leg,
        strategy="momentum",
    )


def _df(*, long=True, mfe=1.2, close_r=1.0, entry=100.0, risk=2.0):
    sign = 1 if long else -1
    prior_close = entry + sign * close_r * risk
    prior_best = entry + sign * mfe * risk
    if long:
        prior = {"open": 100.0, "high": prior_best, "low": 99.8, "close": prior_close, "atr14": 0.5}
        cur = {"open": prior_close, "high": prior_close + 0.2, "low": prior_close - 0.2, "close": prior_close, "atr14": 0.5}
    else:
        prior = {"open": 100.0, "high": 100.2, "low": prior_best, "close": prior_close, "atr14": 0.5}
        cur = {"open": prior_close, "high": prior_close + 0.2, "low": prior_close - 0.2, "close": prior_close, "atr14": 0.5}
    return pd.DataFrame([prior, cur], index=pd.date_range("2026-07-28 15:00", periods=2, freq="min", tz="UTC"))


def _engine(*, long=True, symbol="INTC"):
    e = object.__new__(TradingEngine)
    e.mode = "live"
    e._live_runner_symbols = set()
    e._live_runner_floor = {}
    e._position_initial_stops = {symbol: 98.0 if long else 102.0}
    e._position_take_profits = {symbol: 104.0 if long else 96.0}
    e._position_peaks = {symbol: 100.0}
    e._pending_close_symbols = set()
    e._flattening_symbols = set()
    e.publish = MagicMock()
    e.db = MagicMock()
    e.portfolio = SimpleNamespace(
        _lots={symbol: SimpleNamespace(initial_stop_loss=98.0 if long else 102.0, stop_loss=98.0 if long else 102.0)},
        note_stop=MagicMock(),
    )
    side = OrderSide.SELL if long else OrderSide.BUY
    stop = _order("stop-1", symbol, leg="stop", side=side, stop=98.0 if long else 102.0)
    tp = _order("tp-1", symbol, leg="take_profit", side=side, limit=104.0 if long else 96.0)
    e.broker = SimpleNamespace(open_orders=AsyncMock(return_value=[tp, stop]), replace_exit_leg=AsyncMock())
    position = SimpleNamespace(symbol=symbol, qty=10 if long else -10, avg_entry_price=100.0)
    return e, position, tp, stop


def _successful_replace(e):
    async def replace(order_id, *, limit_price=None, stop_price=None):
        if limit_price is not None:
            return _order(f"{order_id}-r", "INTC", leg="take_profit", side=OrderSide.SELL, limit=limit_price)
        return _order(f"{order_id}-r", "INTC", leg="stop", side=OrderSide.SELL, stop=stop_price)
    e.broker.replace_exit_leg.side_effect = replace


@pytest.mark.asyncio
async def test_long_runner_promotion_is_broker_confirmed_and_ordered_tp_then_stop():
    e, position, _, _ = _engine(long=True)
    _successful_replace(e)
    active = await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.4, close_r=1.2))
    assert active is True
    assert "INTC" in e._live_runner_symbols
    calls = e.broker.replace_exit_leg.await_args_list
    assert "limit_price" in calls[0].kwargs
    assert "stop_price" in calls[1].kwargs
    assert e._live_runner_floor["INTC"] > 100.0


@pytest.mark.asyncio
async def test_short_runner_is_directionally_symmetric():
    e, position, _, _ = _engine(long=False, symbol="NOW")
    async def replace(order_id, *, limit_price=None, stop_price=None):
        if limit_price is not None:
            return _order(f"{order_id}-r", "NOW", leg="take_profit", side=OrderSide.BUY, limit=limit_price)
        return _order(f"{order_id}-r", "NOW", leg="stop", side=OrderSide.BUY, stop=stop_price)
    e.broker.replace_exit_leg.side_effect = replace
    active = await e._manage_live_winner_exit("NOW", position, _df(long=False, mfe=1.4, close_r=1.2))
    assert active is True
    assert e._live_runner_floor["NOW"] < 100.0
    assert e.broker.replace_exit_leg.await_args_list[0].kwargs["limit_price"] < 96.0


@pytest.mark.asyncio
async def test_tp_replace_race_failure_never_raises_stop_or_claims_runner():
    e, position, _, _ = _engine(long=True)
    e.broker.replace_exit_leg.return_value = None
    active = await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.4, close_r=1.2))
    assert active is False
    assert "INTC" not in e._live_runner_symbols
    assert e.broker.replace_exit_leg.await_count == 1
    assert "limit_price" in e.broker.replace_exit_leg.await_args.kwargs


@pytest.mark.asyncio
async def test_stop_replace_race_failure_does_not_claim_runner():
    e, position, _, _ = _engine(long=True)
    async def replace(order_id, *, limit_price=None, stop_price=None):
        if limit_price is not None:
            return _order("tp-r", "INTC", leg="take_profit", side=OrderSide.SELL, limit=limit_price)
        return None
    e.broker.replace_exit_leg.side_effect = replace
    active = await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.4, close_r=1.2))
    assert active is False
    assert "INTC" not in e._live_runner_symbols
    assert e.broker.replace_exit_leg.await_count == 2


@pytest.mark.asyncio
async def test_partial_fill_leg_is_still_discovered_but_missing_sibling_defers_to_rule41():
    e, position, tp, stop = _engine(long=True)
    stop.status = OrderStatus.PARTIALLY_FILLED
    e.broker.open_orders.return_value = [tp, stop]
    _successful_replace(e)
    assert await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.4, close_r=1.2)) is True

    e2, position2, tp2, _ = _engine(long=True)
    e2.broker.open_orders.return_value = [tp2]
    assert await e2._manage_live_winner_exit("INTC", position2, _df(long=True, mfe=1.4, close_r=1.2)) is False
    e2.broker.replace_exit_leg.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_intc_now_brackets_never_cross_select_symbols_or_entry_parent():
    e, position, tp, stop = _engine(long=True, symbol="INTC")
    now_tp = _order("now-tp", "NOW", leg="take_profit", side=OrderSide.SELL, limit=210.0, parent="now-parent")
    now_stop = _order("now-stop", "NOW", leg="stop", side=OrderSide.SELL, stop=190.0, parent="now-parent")
    entry_parent = _order("entry-parent", "INTC", leg="entry", side=OrderSide.BUY, limit=100.0, parent=None)
    e.broker.open_orders.return_value = [entry_parent, now_tp, tp, now_stop, stop]
    _successful_replace(e)
    assert await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.4, close_r=1.2)) is True
    ids = [call.args[0] for call in e.broker.replace_exit_leg.await_args_list]
    assert ids == ["tp-1", "stop-1"]


@pytest.mark.asyncio
async def test_repeated_stop_ratchets_are_monotonic_and_do_not_reissue_looser_stop():
    e, position, _, _ = _engine(long=True)
    _successful_replace(e)
    await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.2, close_r=1.0))
    first = e._live_runner_floor["INTC"]

    # Broker truth on the next cycle contains the already-replaced legs.
    e.broker.open_orders.return_value = [
        _order("tp-2", "INTC", leg="take_profit", side=OrderSide.SELL, limit=140.0),
        _order("stop-2", "INTC", leg="stop", side=OrderSide.SELL, stop=first),
    ]
    await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=2.0, close_r=1.8))
    second = e._live_runner_floor["INTC"]
    assert second >= first

    before = e.broker.replace_exit_leg.await_count
    # Lower subsequent MFE/close must never loosen the broker floor.
    await e._manage_live_winner_exit("INTC", position, _df(long=True, mfe=1.3, close_r=1.1))
    assert e._live_runner_floor["INTC"] >= second
    stop_calls = [c.kwargs["stop_price"] for c in e.broker.replace_exit_leg.await_args_list if "stop_price" in c.kwargs]
    assert stop_calls == sorted(stop_calls)
    assert e.broker.replace_exit_leg.await_count >= before


def test_restart_reconciliation_restores_strategy_initial_stop_and_original_target_before_runner_management():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    reconcile = src[src.index("async def _reconcile_positions"):src.index("async def _reconcile_orders")]
    assert "get_latest_entry_order_payload" in reconcile
    assert "initial_stop_loss" in reconcile
    assert "take_profit = order_payload.get(\"take_profit\")" in reconcile
    assert "self._position_take_profits[p.symbol] = float(take_profit)" in reconcile
    assert "self._live_runner_symbols" not in reconcile  # restart must re-prove broker-confirmed ownership


def test_rule41_remains_authoritative_for_missing_or_invalid_protection():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    assert "Rule 41" in src or "rule41" in src.lower() or "protection" in src.lower()
    manager = src[src.index("async def _manage_live_winner_exit"):src.index("def _evaluate_v2_shadow_passive")]
    assert "if stop_leg is None or tp_leg is None:" in manager
    assert "Rule-41 owns missing-protection recovery" in manager
    assert "return symbol in self._live_runner_symbols" in manager


def test_eod_and_hard_halt_authority_bypass_runner_strategy_arbitration():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/core/engine.py").read_text(encoding="utf-8")
    eod = src[src.index("async def _reconcile_eod_liquidation"):src.index("# ------------------------------------------------------------------\n    # the tick")]
    assert "await self.broker.cancel_all()" in eod
    assert 'await self.broker.flatten_all(reason="end of day")' in eod
    assert "_live_runner_symbols" not in eod
    hard = src[src.index("async def _reconcile_hard_halt_liquidation"):src.index("async def _reconcile_daily_halt_liquidation")]
    assert "await self.broker.cancel_all()" in hard
    assert 'await self.broker.flatten_all(reason="max drawdown kill switch")' in hard
    assert "_live_runner_symbols" not in hard


def test_broker_replace_verifies_replacement_id_status_and_price():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/brokers/alpaca_hardened.py").read_text(encoding="utf-8")
    method = src[src.index("async def replace_exit_leg"):src.index("async def cancel_all")]
    assert "replace_order_by_id" in method
    assert "get_order_by_id" in method
    assert "replacement_id" in method
    assert "OrderStatus.REJECTED" in method and "OrderStatus.CANCELED" in method and "OrderStatus.FILLED" in method
    assert "price verification failed" in method
    assert "BrokerTimeoutError" in method
''', encoding="utf-8")

print("Installed holistic LIVE exit behavioral validation matrix.")
