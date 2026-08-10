"""Shared test fixtures and bar-frame builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.config import RiskConfig, _replay_models_dir
from app.core.clock import MarketClock

@pytest.fixture(autouse=True)
def temp_models_dir(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    token = _replay_models_dir.set(models_dir)
    yield
    _replay_models_dir.reset(token)

# A Tuesday, mid-session: 10:30 ET == 15:30 UTC (EST would be different; this
# date is in EST -- Jan 6, 2026 -- so 10:30 ET == 15:30 UTC).
MID_SESSION = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)


def make_bars(
    n: int = 200,
    start: datetime | None = None,
    price: float = 100.0,
    drift: float = 0.0,
    vol: float = 0.001,
    volume: float = 50_000.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Deterministic 1-minute bar frame anchored to a real session morning."""
    start = start or datetime(2026, 1, 6, 14, 30, tzinfo=UTC)  # 9:30 ET
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    closes = price * np.exp(np.cumsum(rets))
    opens = np.empty(n)
    opens[0] = price
    opens[1:] = closes[:-1]
    spread = np.abs(rng.normal(0, vol, n)) * closes
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    vols = np.full(n, volume) * rng.lognormal(0, 0.2, n)
    idx = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols}, index=idx
    )
    df.index.name = "ts"
    return df


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        risk_per_trade_pct=1.0,
        max_position_pct=50.0,
        max_open_positions=2,
        max_daily_loss_pct=2.0,
        max_drawdown_pct=10.0,
        strategy_consecutive_loss_limit=2,
        strategy_cooldown_minutes=30,
        symbol_cooldown_minutes=30,
        no_entry_minutes_before_close=30,
        flatten_minutes_before_close=5,
        min_price=1.0,
        min_avg_dollar_volume=1000.0,
        ml_confidence_threshold=0.5,
    )


@pytest.fixture
def open_clock() -> MarketClock:
    return MarketClock(always_open=True)


@pytest.fixture
def real_clock() -> MarketClock:
    return MarketClock(always_open=False)
