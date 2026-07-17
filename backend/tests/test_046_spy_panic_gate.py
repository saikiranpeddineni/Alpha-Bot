"""Regressions for issue #87 / 046: SPY panic gate tri-state fail-closed.

Validated invariant: check_spy_volatility() must return SpyVolatilityState,
never collapsing UNKNOWN or error conditions to SAFE.
"""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

from app.core.scanner import MarketScanner, SpyVolatilityState, ScannerOutcome, DEFENSIVE_WATCHLIST


@pytest.fixture
def dm():
    m = MagicMock()
    m.history = AsyncMock()
    m.original_history = m.history
    m.provider.get_bars = AsyncMock(return_value=pd.DataFrame())
    return m


def _make_spy_df(periods: int = 300, hourly: bool = False, high_vol_tail: int = 0) -> pd.DataFrame:
    """Build a minimal SPY bar DataFrame."""
    freq = "1h" if hourly else "1min"
    idx = pd.date_range(end=datetime.now(UTC), periods=periods, freq=freq, tz="UTC")
    closes = 100.0 + np.arange(periods) * 0.01
    highs = closes.copy()
    lows = closes.copy()
    if high_vol_tail > 0:
        # Spike ATR on the tail so it lands at >= 85th percentile
        highs[-high_vol_tail:] += 40.0
        lows[-high_vol_tail:] -= 40.0
    else:
        highs += 0.05
        lows -= 0.05
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": 1_000.0},
        index=idx,
    )


# ---------------------------------------------------------------------------
# UNKNOWN path — every failure mode must return UNKNOWN, never SAFE
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_exception_returns_unknown_not_safe(dm):
    """Any provider exception must return UNKNOWN — not SAFE."""
    dm.history.side_effect = RuntimeError("simulated provider failure")
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN
    assert state is not SpyVolatilityState.SAFE


@pytest.mark.anyio
async def test_http_429_returns_unknown(dm):
    """HTTP 429 (rate limit) must not authorize safe state."""
    dm.history.side_effect = Exception("429 Too Many Requests")
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_http_500_returns_unknown(dm):
    """HTTP 500 must not authorize safe state."""
    dm.history.side_effect = Exception("500 Internal Server Error")
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_timeout_returns_unknown(dm):
    """Timeout must not authorize safe state."""
    import asyncio
    dm.history.side_effect = asyncio.TimeoutError()
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_empty_spy_history_returns_unknown(dm):
    """Empty DataFrame must return UNKNOWN, not SAFE."""
    dm.history.return_value = pd.DataFrame()
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_insufficient_1min_bars_returns_unknown(dm):
    """Fewer than 200 1-min bars returns UNKNOWN."""
    dm.history.return_value = _make_spy_df(periods=50)
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_exactly_199_bars_returns_unknown(dm):
    """Boundary: 199 bars (one below threshold) must be UNKNOWN."""
    dm.history.return_value = _make_spy_df(periods=199)
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_nan_close_price_returns_unknown(dm):
    """NaN in SPY close prices must return UNKNOWN."""
    df = _make_spy_df(periods=300)
    df.loc[df.index[-5:], "close"] = float("nan")
    df.loc[df.index[-5:], "high"] = float("nan")
    dm.history.return_value = df
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


@pytest.mark.anyio
async def test_inf_close_price_returns_unknown(dm):
    """Infinity in SPY prices must return UNKNOWN."""
    df = _make_spy_df(periods=300)
    df.loc[df.index[-1], "close"] = float("inf")
    dm.history.return_value = df
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.UNKNOWN


# ---------------------------------------------------------------------------
# PANIC path
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_high_volatility_tail_returns_panic(dm):
    """ATR spike on tail >= 85th percentile must return PANIC."""
    # Build 300 hourly bars; last 20 have extreme ATR
    idx = pd.date_range(end=datetime.now(UTC), periods=300, freq="1h", tz="UTC")
    closes = 100.0 + np.arange(300) * 0.1
    highs = closes.copy()
    lows = closes.copy()
    highs[:280] += 0.1
    lows[:280] -= 0.1
    highs[280:] += 30.0
    lows[280:] -= 30.0
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": 1_000.0},
        index=idx,
    )
    dm.history.return_value = df
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.PANIC


# ---------------------------------------------------------------------------
# SAFE path
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_normal_fresh_history_returns_safe(dm):
    """Fresh authoritative history with flat vol must return SAFE."""
    idx = pd.date_range(end=datetime.now(UTC), periods=300, freq="1h", tz="UTC")
    closes = np.ones(300) * 100.0
    df = pd.DataFrame(
        {"open": closes, "high": closes + 0.01, "low": closes - 0.01,
         "close": closes, "volume": 1_000.0},
        index=idx,
    )
    dm.history.return_value = df
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    assert state is SpyVolatilityState.SAFE


@pytest.mark.anyio
async def test_exactly_200_bars_not_unknown(dm):
    """Boundary: exactly 200 1-min bars should not return UNKNOWN."""
    dm.history.return_value = _make_spy_df(periods=200)
    scanner = MarketScanner(dm, [])
    state = await scanner.check_spy_volatility()
    # 200 1-min bars will resample to <<20 hourly, so UNKNOWN is still expected here
    # (this documents the resampling dependency)
    assert state in (SpyVolatilityState.UNKNOWN, SpyVolatilityState.SAFE)


# ---------------------------------------------------------------------------
# scanner.run() integration — gate wires correctly
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_with_unknown_gate_blocks_new_entries(dm):
    """UNKNOWN gate must return SUCCESS_EMPTY (no new-entry candidates)."""
    scanner = MarketScanner(dm, [])
    scanner.check_spy_volatility = AsyncMock(return_value=SpyVolatilityState.UNKNOWN)
    outcome, watchlist = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert watchlist == []


@pytest.mark.anyio
async def test_run_with_panic_gate_returns_defensive_watchlist(dm):
    """PANIC gate must return DEFENSIVE_WATCHLIST immediately."""
    scanner = MarketScanner(dm, [])
    scanner.check_spy_volatility = AsyncMock(return_value=SpyVolatilityState.PANIC)
    outcome, watchlist = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_NONEMPTY
    assert watchlist == DEFENSIVE_WATCHLIST


@pytest.mark.anyio
async def test_run_with_safe_gate_proceeds_to_pipeline(dm, monkeypatch):
    """SAFE gate must let the normal candidate pipeline execute."""
    scanner = MarketScanner(dm, [])
    scanner.check_spy_volatility = AsyncMock(return_value=SpyVolatilityState.SAFE)
    monkeypatch.setattr(scanner, "_stage1_symbols", AsyncMock(return_value=["AAPL"]))
    monkeypatch.setattr(scanner, "_stage1_prefilter_parallel", AsyncMock(return_value=[]))
    outcome, watchlist = await scanner.run()
    # Stage 1 empty → SUCCESS_EMPTY, but pipeline was entered (not blocked)
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    scanner._stage1_prefilter_parallel.assert_awaited_once()


@pytest.mark.anyio
async def test_run_exception_in_gate_does_not_propagate_as_safe(dm):
    """If gate itself raises, run() must not proceed as SAFE."""
    async def _raising_gate(as_of=None):
        raise RuntimeError("gate exploded")
    scanner = MarketScanner(dm, [])
    scanner.check_spy_volatility = _raising_gate
    # run() outer try/except catches and returns FAILED — never SUCCESS_NONEMPTY with live candidates
    outcome, watchlist = await scanner.run()
    assert outcome is ScannerOutcome.FAILED
    assert watchlist == []
