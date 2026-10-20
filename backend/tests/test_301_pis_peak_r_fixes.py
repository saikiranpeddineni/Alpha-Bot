"""Regression tests for PIS peak-R accounting, runner protection, and recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.domain import Position
from app.db.database import Database
from app.pis.engine_adapter import PISEngineAdapter
from app.pis.models import (
    ExitAction,
    LifecycleState,
    PISConfig,
    PositionObservation,
    TradeHealth,
)
from app.pis.position_brain import PositionBrain
from app.pis.protection import AdaptiveProtectionPlanner


def _obs(
    *,
    symbol: str = "TEST",
    side: float = 1.0,
    entry: float = 100.0,
    initial_stop: float = 95.0,
    current: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    atr: float = 2.0,
    timestamp: datetime = datetime(2026, 10, 17, 13, 30, tzinfo=UTC),
) -> PositionObservation:
    risk = abs(entry - initial_stop)
    high = max(current, entry) if high is None else high
    low = min(current, entry) if low is None else low
    close = current if close is None else close
    return PositionObservation(
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        qty=100.0,
        entry_price=entry,
        current_price=current,
        high=high,
        low=low,
        close=close,
        volume=10_000.0,
        atr=atr,
        initial_stop=initial_stop,
        risk_per_share=risk,
        current_stop=initial_stop,
        r_multiple=(current - entry) / risk * side,
    )


def test_adapter_keeps_r_geometry_tied_to_actual_initial_stop() -> None:
    """A tight broker stop must not be silently widened into a different R unit."""
    adapter = PISEngineAdapter()
    adapter.persist_profit_memory = False
    position = SimpleNamespace(
        symbol="TIGHT",
        qty=10.0,
        avg_entry_price=100.0,
        current_price=99.90,
        market_price=99.90,
        opened_at=datetime(2026, 10, 17, 13, 30, tzinfo=UTC),
        strategy="manual",
    )
    bars = pd.DataFrame(
        [{"high": 100.05, "low": 99.80, "close": 99.90, "volume": 1_000.0, "atr": 1.0}]
    )

    import asyncio

    asyncio.run(
        adapter.evaluate_position_exit(
            position=position,
            bar_df=bars,
            initial_stop=99.80,
        )
    )

    brain = adapter.service.get_brain("TIGHT")
    assert brain is not None
    observed = brain.history[-1]
    assert observed.risk_per_share == pytest.approx(0.20)
    assert observed.r_multiple == pytest.approx(-0.50)


@pytest.mark.asyncio
async def test_adapter_keeps_runner_extremes_on_completed_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresher broker quote must not become a future bar high/low."""
    monkeypatch.setattr(Database, "active", staticmethod(lambda: None))
    adapter = PISEngineAdapter()
    position = Position(
        symbol="BAR",
        qty=10.0,
        avg_entry_price=100.0,
        market_price=120.0,
    )
    bars = pd.DataFrame(
        [{"high": 110.0, "low": 99.0, "close": 109.0, "volume": 1_000.0, "atr": 2.0}]
    )

    await adapter.evaluate_position_exit(
        position=position,
        bar_df=bars,
        initial_stop=95.0,
    )

    brain = adapter.service.get_brain("BAR")
    assert brain is not None
    observed = brain.history[-1]
    assert observed.high == pytest.approx(110.0)
    assert observed.low == pytest.approx(99.0)


def test_scaled_out_runner_uses_completed_bar_extreme_with_atr_buffer() -> None:
    """Runner protection anchors to the completed bar extreme, not its close."""
    planner = AdaptiveProtectionPlanner(PISConfig())
    health = TradeHealth(
        overall=0.80,
        trend_score=0.80,
        momentum_score=0.80,
        volatility_score=0.80,
        volume_score=0.80,
    )

    long_bar = _obs(current=105.0, high=110.0, low=104.0, close=105.0)
    long_floor = planner.compute_protection_floor(
        long_bar,
        peak_r=2.0,
        health=health,
        previous_floor=95.0,
        has_scaled_out=True,
        predicted_max_r=5.0,
    )
    # Early runner phase: completed-bar high minus 2x ATR, with the R floor as a cap.
    assert long_floor == pytest.approx(106.0)

    short_bar = _obs(
        side=-1.0,
        initial_stop=105.0,
        current=95.0,
        high=96.0,
        low=90.0,
        close=95.0,
    )
    short_floor = planner.compute_protection_floor(
        short_bar,
        peak_r=2.0,
        health=health,
        previous_floor=105.0,
        has_scaled_out=True,
        predicted_max_r=5.0,
    )
    # Short runner protection uses the completed-bar low plus the same ATR buffer.
    assert short_floor == pytest.approx(94.0)

    # A close-only change cannot move a completed-bar-extreme anchored floor.
    same_extreme_later_close = _obs(current=108.0, high=110.0, low=104.0, close=108.0)
    assert planner.compute_protection_floor(
        same_extreme_later_close,
        peak_r=2.0,
        health=health,
        previous_floor=95.0,
        has_scaled_out=True,
        predicted_max_r=5.0,
    ) == pytest.approx(long_floor)


def test_partial_exit_decision_reports_recomputed_runner_floor() -> None:
    """The PARTIAL_EXIT decision and brain state must expose the same runner floor."""
    config = PISConfig(scale_out_target_r=1.0)
    initial = _obs(timestamp=datetime(2026, 10, 17, 13, 30, tzinfo=UTC))
    brain = PositionBrain("TEST", initial, config)

    decision = brain.observe(
        _obs(
            current=105.0,
            high=110.0,
            low=100.0,
            close=105.0,
            timestamp=datetime(2026, 10, 17, 13, 31, tzinfo=UTC),
        )
    )

    assert decision.action == ExitAction.PARTIAL_EXIT
    assert brain.has_scaled_out is True
    assert decision.target_stop_loss == pytest.approx(brain.current_stop_floor)


class _MemoryDatabase:
    """Small database double for the adapter's existing JSON payload store."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.saved_payload: dict | None = None

    def get_pis_profit_memory(self, *, mode: str, symbol: str) -> dict:
        return dict(self.payload)

    def save_pis_profit_memory(self, *, mode: str, symbol: str, payload: dict) -> None:
        self.saved_payload = payload


@pytest.mark.asyncio
async def test_adapter_restores_full_pis_state_from_existing_payload_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restart recovery restores floor, runner flags, and lifecycle together."""
    db = _MemoryDatabase(
        {
            "symbol": "RESTORE",
            "entry_date": "2026-10-17",
            "entry_price": 100.0,
            "side": 1.0,
            "initial_stop": 95.0,
            "peak_r": 2.0,
            "trough_r": -0.10,
            "mfe_price": 110.0,
            "mae_price": 99.5,
            "current_stop_floor": 104.0,
            "has_scaled_out": True,
            "runner_qualified": True,
            "lifecycle": LifecycleState.RUNNER.value,
            "stagnation_counter": 0,
            "degradation_counter": 0,
        }
    )
    monkeypatch.setattr(Database, "active", staticmethod(lambda: db))

    adapter = PISEngineAdapter()
    position = Position(
        symbol="RESTORE",
        qty=50.0,
        avg_entry_price=100.0,
        market_price=106.0,
        opened_at=datetime(2026, 10, 17, 13, 30, tzinfo=UTC),
    )
    bars = pd.DataFrame(
        [{"high": 106.0, "low": 104.0, "close": 106.0, "volume": 5_000.0, "atr": 1.0}]
    )

    _, decision = await adapter.evaluate_position_exit(
        position=position,
        bar_df=bars,
        initial_stop=95.0,
        timestamp=datetime(2026, 10, 17, 13, 31, tzinfo=UTC),
    )

    brain = adapter.service.get_brain("RESTORE")
    assert brain is not None
    assert brain.has_scaled_out is True
    assert brain.lifecycle == LifecycleState.RUNNER
    assert brain.current_stop_floor >= 104.0
    assert brain.serialize_state()["runner_qualified"] is True
    assert decision.peak_r >= 2.0
    assert db.saved_payload is not None
    assert db.saved_payload["has_scaled_out"] is True


@pytest.mark.asyncio
async def test_adapter_restores_identity_bound_state_when_broker_has_no_opened_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact position identity is sufficient when a restart loses opened_at."""
    db = _MemoryDatabase(
        {
            "symbol": "RESTORE",
            "entry_date": None,
            "entry_price": 100.0,
            "side": 1.0,
            "initial_stop": 95.0,
            "peak_r": 2.0,
            "trough_r": -0.10,
            "mfe_price": 110.0,
            "mae_price": 99.5,
            "current_stop_floor": 104.0,
            "has_scaled_out": True,
            "runner_qualified": True,
            "lifecycle": LifecycleState.RUNNER.value,
        }
    )
    monkeypatch.setattr(Database, "active", staticmethod(lambda: db))

    adapter = PISEngineAdapter()
    position = Position(
        symbol="RESTORE",
        qty=50.0,
        avg_entry_price=100.0,
        market_price=106.0,
        opened_at=None,
    )
    bars = pd.DataFrame(
        [{"high": 106.0, "low": 104.0, "close": 106.0, "volume": 5_000.0, "atr": 1.0}]
    )

    _, decision = await adapter.evaluate_position_exit(
        position=position,
        bar_df=bars,
        initial_stop=95.0,
        timestamp=datetime(2026, 10, 17, 13, 31, tzinfo=UTC),
    )

    brain = adapter.service.get_brain("RESTORE")
    assert brain is not None
    assert brain.has_scaled_out is True
    assert brain.current_stop_floor >= 104.0
    assert decision.peak_r >= 2.0
