"""Backtest engine end-to-end tests on deterministic synthetic data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest
from app.backtest.metrics import compute_metrics, max_drawdown_pct
from app.core.domain import TradeRecord
from app.data.manager import DataManager
from app.data.synthetic import SyntheticProvider, generate_day


@pytest.fixture
def data(tmp_path) -> DataManager:
    return DataManager(SyntheticProvider(), tmp_path / "cache.db")


@pytest.fixture
def model(tmp_path) -> SignalModel:
    return SignalModel(tmp_path / "models")  # untrained -> heuristic gate


def test_generate_day_is_deterministic():
    d = datetime(2026, 1, 6).date()
    a = generate_day("SPY", d)
    b = generate_day("SPY", d)
    assert a.equals(b)
    assert len(a) == 390
    assert (a["high"] >= a[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (a["low"] <= a[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (a["volume"] > 0).all()


async def test_backtest_runs_and_reports(data, model, risk_config):
    params = BacktestParams(
        symbols=["SPY", "NVDA"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        # Scanner behavior is covered by the scanner/replay suites. Keep this
        # engine accounting smoke test bounded to its requested universe.
        disable_scanner=True,
        risk=risk_config,
    )
    result = await run_backtest(data, model, params)
    assert result.bars_processed > 1000  # ~390 one-minute replay ticks per session
    assert len(result.equity_curve) > 100
    m = result.metrics
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe", "win_rate", "profit_factor",
                "total_trades", "expectancy", "final_equity"):
        assert key in m
    # The frame-advance regression guard: a frozen replay window produces 0
    # trades while looking perfectly "healthy". Synthetic sessions must trade.
    assert m["total_trades"] >= 1, "replay produced zero trades -- frame advance is likely broken"
    # Equity stays positive and drawdown respected the 10% kill switch bound (with slack for gaps)
    assert m["final_equity"] > 0
    assert m["max_drawdown_pct"] < 25.0
    for trade in result.trades:
        assert isinstance(trade, TradeRecord)
        assert trade.exit_at >= trade.entry_at
        assert trade.qty > 0


async def test_backtest_no_overnight_positions(data, model, risk_config):
    """Every trade must close the same session it opened (day trading contract)."""
    params = BacktestParams(
        symbols=["AAPL"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
        disable_scanner=True,
        risk=risk_config,
    )
    result = await run_backtest(data, model, params)
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    for trade in result.trades:
        assert trade.entry_at.astimezone(et).date() == trade.exit_at.astimezone(et).date(), (
            f"overnight hold detected: {trade}"
        )


# =============================================================================
# [MODIFIED 2026-10-12]: Isolate ML gate test from daily profit lock shutdown
# ORIGINAL INTENT: Verify that turning off the ML gate produces >= trades than gated.
# REASON FOR CHANGE: When the ungated run achieves higher early profits, daily_profit_lock
# triggers and flattens/halts trading for the rest of the day, resulting in fewer total trades.
# Setting enable_daily_profit_lock=False isolates the test strictly to the ML gating filter.
# SAFE ROLLBACK: The original code is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# async def test_backtest_ml_gate_off_trades_at_least_as_much(data, model, risk_config):
#     kwargs = dict(
#         symbols=["SPY"],
#         start=datetime(2026, 1, 5, tzinfo=UTC),
#         end=datetime(2026, 1, 7, 23, 59, tzinfo=UTC),
#         risk=risk_config,
#     )
#     gated = await run_backtest(data, model, BacktestParams(use_ml_gate=True, **kwargs))
#     ungated = await run_backtest(data, model, BacktestParams(use_ml_gate=False, **kwargs))
#     assert ungated.metrics["total_trades"] >= gated.metrics["total_trades"]
# -----------------------------------------------------------------------------
# =============================================================================
# [MODIFIED 2026-10-13]: Isolate ML gate test from ticker cooldown lockout
# ORIGINAL INTENT: Verify that ungated backtest produces >= trades than ML-gated.
# REASON FOR CHANGE: When an ungated trade takes a stop loss on the single symbol SPY,
# the 30-minute cooldown locks SPY, causing ungated to miss subsequent entries and fail
# the assertion. Disabling ticker cooldown isolates the test strictly to the ML gate.
# SAFE ROLLBACK: Previous code preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# async def test_backtest_ml_gate_off_trades_at_least_as_much(data, model, risk_config):
#     test_risk = risk_config.model_copy(update={"enable_daily_profit_lock": False})
#     kwargs = dict(
#         symbols=["SPY"],
#         start=datetime(2026, 1, 5, tzinfo=UTC),
#         end=datetime(2026, 1, 7, 23, 59, tzinfo=UTC),
#         risk=test_risk,
#     )
#     gated = await run_backtest(data, model, BacktestParams(use_ml_gate=True, **kwargs))
#     ungated = await run_backtest(data, model, BacktestParams(use_ml_gate=False, **kwargs))
#     assert ungated.metrics["total_trades"] >= gated.metrics["total_trades"]
# -----------------------------------------------------------------------------
# [NEW CODE]:
async def test_backtest_ml_gate_off_trades_at_least_as_much(data, model, risk_config):
    test_risk = risk_config.model_copy(update={
        "enable_daily_profit_lock": False,
        "symbol_cooldown_minutes": 0,
        "symbol_max_daily_losses": 10,
    })
    kwargs = dict(
        symbols=["SPY"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 7, 23, 59, tzinfo=UTC),
        disable_scanner=True,
        risk=test_risk,
    )
    gated = await run_backtest(data, model, BacktestParams(use_ml_gate=True, **kwargs))
    ungated = await run_backtest(data, model, BacktestParams(use_ml_gate=False, **kwargs))
    assert ungated.metrics["total_trades"] >= gated.metrics["total_trades"]
# =============================================================================
# =============================================================================


async def test_backtest_rejects_empty_range(data, model):
    params = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 1, 3, tzinfo=UTC),  # Saturday
        end=datetime(2026, 1, 4, 23, 0, tzinfo=UTC),  # Sunday
    )
    with pytest.raises(ValueError):
        await run_backtest(data, model, params)


def test_metrics_math():
    t0 = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)
    from datetime import timedelta

    curve = [(t0 + timedelta(days=i), eq) for i, eq in enumerate([100.0, 110.0, 99.0, 120.0])]
    trades = [
        TradeRecord(symbol="A", side="long", qty=1, entry_price=1, exit_price=2, entry_at=t0,
                    exit_at=t0, pnl=10.0, pnl_pct=1.0, r_multiple=2.0, strategy="t"),
        TradeRecord(symbol="A", side="long", qty=1, entry_price=2, exit_price=1, entry_at=t0,
                    exit_at=t0, pnl=-5.0, pnl_pct=-0.5, r_multiple=-1.0, strategy="t"),
    ]
    m = compute_metrics(curve, trades, 100.0)
    assert m["total_return_pct"] == pytest.approx(20.0)
    assert m["max_drawdown_pct"] == pytest.approx(10.0)
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == 2.0
    assert m["avg_r"] == pytest.approx(0.5)
    assert m["expectancy"] == pytest.approx(2.5)


def test_max_drawdown_empty():
    import pandas as pd

    assert max_drawdown_pct(pd.Series(dtype=float)) == 0.0
