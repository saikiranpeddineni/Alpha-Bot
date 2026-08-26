"""RecentPerformanceGovernor behavior tests."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import RiskConfig
from app.portfolio.recent_performance import RecentPerformanceGovernor


def _cfg(**over) -> RiskConfig:
    base = dict(
        governor_enabled=True,
        governor_lookback_days=14,
        governor_min_trades=4,
        governor_shrink_k=2.0,
        governor_loss_scale=3.0,
        governor_floor=0.25,
        governor_refresh_minutes=15,
    )
    base.update(over)
    return RiskConfig(**base)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def get_recent_trade_contexts(self, lookback_days):
        self.calls += 1
        return self._rows


NOW = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)


def _row(sym: str, strat: str, r: float):
    return {"symbol": sym, "strategy": strat, "r_multiple": r, "exit_at_ts": NOW}


def test_losing_symbol_and_hour_get_scaled_down():
    rows = [_row("INTC", "orb", -1.0)] * 6 + [_row("NVDA", "momentum", 1.0)] * 6 + [_row("X", "s", 1.0)] * 6 + [_row("X", "s", -1.0)] * 6
    db = _FakeDB(rows)
    gov = RecentPerformanceGovernor(db, lambda: _cfg())
    f_intc = gov.factor_for("INTC", "unknown_strat", NOW.replace(hour=10))
    assert f_intc < 1.0, "persistent loser must be trimmed"
    assert db.calls == 1


def test_never_amplifies_and_respects_floor():
    rows = [_row("BE", "orb", -2.5)] * 20  # deeply negative context
    gov = RecentPerformanceGovernor(_FakeDB(rows), lambda: _cfg(governor_floor=0.25))
    f = gov.factor_for("BE", "orb", NOW)
    assert 0.25 <= f < 1.0
    # Positive context must be exactly neutral (never > 1)
    rows_pos = [_row("NVDA", "m", 3.0)] * 20
    gov2 = RecentPerformanceGovernor(_FakeDB(rows_pos), lambda: _cfg())
    assert gov2.factor_for("NVDA", "m", NOW) == 1.0


def test_thin_samples_do_not_trigger():
    rows = [_row("MDB", "s", -3.0)] * 3  # below min_trades
    gov = RecentPerformanceGovernor(_FakeDB(rows), lambda: _cfg())
    assert gov.factor_for("MDB", "s", NOW) == 1.0


def test_cache_prevents_refetch_within_ttl():
    rows = [_row("A", "s", -1.0)] * 8
    db = _FakeDB(rows)
    gov = RecentPerformanceGovernor(db, lambda: _cfg())
    gov.factor_for("A", "s", NOW)
    calls_after_first = db.calls
    gov.factor_for("A", "s", NOW.replace(minute=5))
    assert db.calls == calls_after_first


def test_db_failure_degrades_to_neutral():
    class BoomDB:
        def get_recent_trade_contexts(self, days):
            raise RuntimeError("db down")

    gov = RecentPerformanceGovernor(BoomDB(), lambda: _cfg())
    assert gov.factor_for("AAPL", "x", NOW) == 1.0
