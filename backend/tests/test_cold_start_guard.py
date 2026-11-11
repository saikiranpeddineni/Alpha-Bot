"""Unit tests for Opening Cold-Start Bar Starvation Guard (R3)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from app.core.domain import Regime, Signal, SignalAction
from app.strategies.pipeline import SignalPipeline


class DummyModel:
    def score_action(self, features, action):
        return 0.60


class MockShadowLedger:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def _make_bars_at_time(start_str: str, n_bars: int) -> pd.DataFrame:
    et = ZoneInfo("America/New_York")
    times = pd.date_range(start_str, periods=n_bars, freq="1min", tz=et)
    return pd.DataFrame(
        {
            "open": [150.0] * n_bars,
            "high": [151.0] * n_bars,
            "low": [149.0] * n_bars,
            "close": [150.5] * n_bars,
            "volume": [2000.0] * n_bars,
            "atr14": [1.2] * n_bars,
            "vol_sma20": [2000.0] * n_bars,
        },
        index=times,
    )


def test_cold_start_suppresses_entry_before_945_with_few_bars():
    """Before 09:45 AM ET with < 15 intraday bars, candidate entry signals must be suppressed."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    ledger = MockShadowLedger()

    # 5 intraday bars at 09:35 AM ET
    df = _make_bars_at_time("2026-01-06 09:31:00", 5)
    now = datetime(2026, 1, 6, 9, 35, tzinfo=ZoneInfo("America/New_York"))

    sig = Signal(
        symbol="TSLA",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test_cold_start",
        price=150.5,
        stop_loss=148.0,
        take_profit=155.0,
    )

    accepted, rejected = pipeline.process(
        symbol="TSLA",
        df=df,
        signals=[sig],
        regime=Regime.TREND_UP,
        now=now,
        shadow_ledger=ledger,
    )

    assert len(accepted) == 0
    assert len(rejected) == 1
    rejected_sig, reason = rejected[0]
    assert rejected_sig.warmup_pending is True
    assert "warmup_pending" in reason
    assert "15" in reason and "before 09:45" in reason

    # Shadow ledger check
    assert len(ledger.records) == 1
    assert ledger.records[0]["gate"] == "warmup_pending"
    assert ledger.records[0]["symbol"] == "TSLA"


def test_cold_start_allows_entry_with_at_least_15_bars():
    """Before 09:45 AM ET with >= 15 intraday bars, candidate signals pass cold-start guard."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    df = _make_bars_at_time("2026-01-06 09:30:00", 15)
    now = datetime(2026, 1, 6, 9, 44, tzinfo=ZoneInfo("America/New_York"))

    sig = Signal(
        symbol="TSLA",
        action=SignalAction.ENTER_LONG,
        confidence=0.85,
        strategy="orb",
        reason="test_warm",
        price=150.5,
        stop_loss=148.0,
        take_profit=155.0,
    )

    accepted, rejected = pipeline.process(
        symbol="TSLA",
        df=df,
        signals=[sig],
        regime=Regime.TREND_UP,
        now=now,
    )

    # Signal should pass cold-start
    assert not any(getattr(s, "warmup_pending", False) for s, _ in rejected)
    assert any(s.action == SignalAction.ENTER_LONG for s in accepted)


def test_cold_start_exempts_protective_exits():
    """Protective exits must NEVER be blocked by cold-start bar starvation."""
    pipeline = SignalPipeline(model=DummyModel(), ml_threshold=0.0)
    df = _make_bars_at_time("2026-01-06 09:31:00", 3)
    now = datetime(2026, 1, 6, 9, 33, tzinfo=ZoneInfo("America/New_York"))

    sig_exit = Signal(
        symbol="TSLA",
        action=SignalAction.EXIT,
        confidence=1.0,
        strategy="orb",
        reason="stop_loss",
        price=150.0,
    )
    sig_scale = Signal(
        symbol="TSLA",
        action=SignalAction.SCALE_OUT_HALF,
        confidence=1.0,
        strategy="orb",
        reason="take_profit_half",
        price=150.0,
    )

    accepted, rejected = pipeline.process(
        symbol="TSLA",
        df=df,
        signals=[sig_exit, sig_scale],
        regime=Regime.VOLATILE,
        now=now,
    )

    assert len(rejected) == 0
    assert len(accepted) == 2
    assert any(s.action == SignalAction.EXIT for s in accepted)
    assert any(s.action == SignalAction.SCALE_OUT_HALF for s in accepted)
