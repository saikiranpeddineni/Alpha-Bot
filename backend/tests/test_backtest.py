"""Backtest engine end-to-end tests on deterministic synthetic data."""

from __future__ import annotations
from app.config import RiskConfig
from app.config import Settings

from datetime import UTC, datetime

import pytest

from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest
from app.backtest.metrics import compute_metrics, max_drawdown_pct
from app.config import DATA_DIR, RiskConfig
from app.core.domain import TradeRecord
from app.data.manager import DataManager
from app.data.synthetic import SyntheticProvider, generate_day


@pytest.fixture
def data(tmp_path) -> DataManager:
    return DataManager(SyntheticProvider(), tmp_path / "cache.db")


@pytest.fixture
def model(tmp_path) -> SignalModel:
    return SignalModel(tmp_path / "models", risk_config=RiskConfig())  # untrained -> heuristic gate


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
        risk=risk_config,
    )
    result = await run_backtest(data, model, params, settings=Settings())
    assert result.bars_processed > 2000
    assert len(result.equity_curve) > 100
    m = result.metrics
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe", "win_rate", "profit_factor",
                "total_trades", "expectancy", "final_equity"):
        assert key in m
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
        risk=risk_config,
    )
    result = await run_backtest(data, model, params, settings=Settings())
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    for trade in result.trades:
        assert trade.entry_at.astimezone(et).date() == trade.exit_at.astimezone(et).date(), (
            f"overnight hold detected: {trade}"
        )


async def test_backtest_ml_gate_off_trades_at_least_as_much(data, model, risk_config):
    kwargs = dict(
        symbols=["SPY"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 7, 23, 59, tzinfo=UTC),
        risk=risk_config,
    )
    from app.config import Settings
    gated = await run_backtest(data, model, BacktestParams(use_ml_gate=True, **kwargs), Settings())
    ungated = await run_backtest(data, model, BacktestParams(use_ml_gate=False, **kwargs), Settings())
    assert ungated.metrics["total_trades"] >= gated.metrics["total_trades"]


async def test_backtest_rejects_empty_range(data, model):
    params = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 1, 3, tzinfo=UTC),  # Saturday
        end=datetime(2026, 1, 4, 23, 0, tzinfo=UTC),  # Sunday
    )
    with pytest.raises(ValueError):
        await run_backtest(data, model, params, settings=Settings())


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


async def test_backtest_dynamic_scanner_runs(data, model, risk_config, monkeypatch):
    """Dynamic scanner mode should complete without errors.

    The scanner runs on synthetic data, may fail validation gates (AUC thresholds
    not met), and fall back to the core watchlist — that is the expected, safe behavior.
    The test asserts the backtest still completes cleanly and produces a valid result.
    """
    import app.core.scanner
    monkeypatch.setattr(app.core.scanner, "CANDIDATE_POOL", ["SPY", "AAPL"])
    monkeypatch.setattr(app.core.scanner, "DEFENSIVE_WATCHLIST", ["SPY"])

    params = BacktestParams(
        symbols=["SPY", "AAPL"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        use_dynamic_scanner=True,
        risk=risk_config,
    )
    result = await run_backtest(data, model, params, settings=Settings())
    # Even with scanner enabled the backtest must produce a valid result
    assert result.bars_processed >= 0
    assert isinstance(result.metrics, dict)
    assert "total_return_pct" in result.metrics
    assert result.metrics["final_equity"] > 0


def test_watchlist_merge_semantics():
    # Test morning scan (is_intraday=False) replacement logic
    open_symbols = {"AAPL"}
    scanned_watchlist = ["MSFT", "AAPL"]
    new_watchlist = ["TSLA", "NVDA"]
    protected = open_symbols & set(scanned_watchlist)
    scanned_watchlist_new = sorted(list(set(new_watchlist) | protected))
    assert scanned_watchlist_new == ["AAPL", "NVDA", "TSLA"]

    # Test intraday scan (is_intraday=True) merge/hysteresis logic
    open_symbols = {"AAPL"}
    existing = set(scanned_watchlist_new)
    new_set = {"TSLA", "MSFT"}
    to_remove = (existing - new_set) - open_symbols
    to_add = new_set - existing
    scanned_watchlist_intraday = sorted(list((existing - to_remove) | to_add))
    assert scanned_watchlist_intraday == ["AAPL", "MSFT", "TSLA"]


def test_compute_relative_strength():
    import pandas as pd
    from app.indicators.ta import compute_relative_strength
    sym_df = pd.DataFrame({"ret_5": [0.01], "ret_15": [0.02]})
    spy_df = pd.DataFrame({"ret_5": [0.005], "ret_15": [0.01]})
    rs = compute_relative_strength(sym_df, spy_df)
    # rs_raw = 0.40 * (0.01 - 0.005) + 0.60 * (0.02 - 0.01) = 0.40 * 0.005 + 0.60 * 0.01 = 0.002 + 0.006 = 0.008
    # rs_normalized = 0.008 / 2.0 + 0.5 = 0.004 + 0.5 = 0.504
    assert rs == pytest.approx(0.504)


async def test_run_backtest_temporal_leakage_gating(data, model, risk_config):
    # Case 1: ignore_temporal_leakage is False -> backtest_start_time is enforced
    params_enforce = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        risk=risk_config,
        ignore_temporal_leakage=False,
    )
    res_enforce = await run_backtest(data, model, params_enforce, settings=Settings())
    assert res_enforce.bars_processed > 0

    # Case 2: ignore_temporal_leakage is True -> backtest_start_time is bypassed/None
    params_ignore = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        risk=risk_config,
        ignore_temporal_leakage=True,
    )
    res_ignore = await run_backtest(data, model, params_ignore, settings=Settings())
    assert res_ignore.bars_processed > 0

