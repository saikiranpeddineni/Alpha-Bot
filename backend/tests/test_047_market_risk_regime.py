"""CR-047 — VIX-based MarketRiskState tests.

All tests are deterministic, use only injected mocks, and make no real network calls.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.risk.market_regime import (
    MarketRiskAggregator,
    MarketRiskState,
    SignalHealth,
)
from app.core.scanner import MarketScanner, ScannerOutcome, SpyVolatilityState


def _make_vix_df(vix_value: float, n: int = 10) -> pd.DataFrame:
    """Minimal 1-min OHLCV frame for VIX."""
    idx = pd.date_range("2025-01-02 09:30", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "open": [vix_value] * n,
        "high": [vix_value] * n,
        "low": [vix_value] * n,
        "close": [vix_value] * n,
        "volume": [1000] * n,
    }, index=idx)
    return df


def _aggregator_with_mock(
    vix_value: float | None = 15.0,
    raises: Exception | None = None,
) -> MarketRiskAggregator:
    """Build a MarketRiskAggregator with fully injected mock data manager."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if raises:
            raise raises
        if vix_value is None:
            return pd.DataFrame()
        return _make_vix_df(vix_value)

    dm = SimpleNamespace(history=_history)
    return MarketRiskAggregator(data_manager=dm)


# ---------------------------------------------------------------------------
# AC1 — VIX primary query unavailable, yfinance fallback works
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac1_primary_unavailable_fallback_works():
    """If primary provider fails, fallback to yfinance works."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        raise ConnectionError("primary provider timeout")

    dm = SimpleNamespace(history=_history)
    agg = MarketRiskAggregator(data_manager=dm)

    # Mock yfinance.download
    mock_df = pd.DataFrame({"Close": [18.5]}, index=[pd.Timestamp("2025-01-02 10:00:00", tz="UTC")])
    with patch("yfinance.download", return_value=mock_df) as mock_download:
        state, conf, cov, reasons, healths = await agg.assess()
        assert state is MarketRiskState.NORMAL
        assert conf == 1.0
        assert cov == 1.0
        assert "vix" in healths
        assert healths["vix"].healthy
        assert "18.5" in healths["vix"].reason
        mock_download.assert_called()


# ---------------------------------------------------------------------------
# AC2 — VIX threshold mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac2_vix_thresholds():
    """Verify that different VIX values map to correct MarketRiskStates."""
    # VIX < 20 -> NORMAL
    agg_normal = _aggregator_with_mock(vix_value=15.0)
    state, _, _, _, _ = await agg_normal.assess()
    assert state is MarketRiskState.NORMAL

    # 20 <= VIX < 30 -> CAUTION
    agg_caution = _aggregator_with_mock(vix_value=25.0)
    state, _, _, _, _ = await agg_caution.assess()
    assert state is MarketRiskState.CAUTION

    # VIX >= 30 -> PANIC
    agg_panic = _aggregator_with_mock(vix_value=35.0)
    state, _, _, _, _ = await agg_panic.assess()
    assert state is MarketRiskState.PANIC


# ---------------------------------------------------------------------------
# AC3 — Conflicting/Deterministic state with reason codes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac3_deterministic_with_reason_codes():
    """VIX must produce deterministic state and reason codes for identical inputs."""
    agg = _aggregator_with_mock(vix_value=25.0)
    state1, conf1, cov1, reasons1, _ = await agg.assess()
    state2, conf2, cov2, reasons2, _ = await agg.assess()
    assert state1 == state2 == MarketRiskState.CAUTION
    assert conf1 == conf2 == 1.0
    assert cov1 == cov2 == 1.0
    assert reasons1 == reasons2 == ["VIX:CAUTION(25.00)"]


# ---------------------------------------------------------------------------
# AC4 — Insufficient coverage (all fail) -> UNKNOWN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac4_all_fail_produces_unknown():
    """When both primary provider and yfinance fail, state is UNKNOWN."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        raise ConnectionError("primary failed")

    dm = SimpleNamespace(history=_history)
    agg = MarketRiskAggregator(data_manager=dm)

    with patch("yfinance.download", side_effect=RuntimeError("yfinance failed")):
        state, conf, cov, reasons, healths = await agg.assess()
        assert state is MarketRiskState.UNKNOWN
        assert conf == 0.0
        assert cov == 0.0
        assert reasons == ["VIX_UNAVAILABLE"]
        assert not healths["vix"].healthy


# ---------------------------------------------------------------------------
# AC6 — Provider error cannot cast SAFE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    ConnectionError("429 too many requests"),
    ConnectionError("500 internal server error"),
    PermissionError("401 unauthorized"),
    ValueError("malformed response frame"),
    TimeoutError("request timed out"),
])
@pytest.mark.asyncio
async def test_ac6_provider_error_cannot_cast_safe(exc):
    """Any provider error must result in UNKNOWN state, never NORMAL/SAFE."""
    agg = _aggregator_with_mock(raises=exc)
    with patch("yfinance.download", side_effect=exc):
        state, conf, cov, _, healths = await agg.assess()
        assert state is MarketRiskState.UNKNOWN
        assert not healths["vix"].healthy
        assert conf == 0.0


# ---------------------------------------------------------------------------
# AC7 — UNKNOWN blocks entries; scanner returns SUCCESS_EMPTY
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac7_unknown_blocks_new_entries():
    """When regime is UNKNOWN scanner.run() must return SUCCESS_EMPTY, []"""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        raise ConnectionError("outage")

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])
    with patch("yfinance.download", side_effect=RuntimeError("down")):
        outcome, watchlist = await scanner.run()
        assert outcome is ScannerOutcome.SUCCESS_EMPTY
        assert watchlist == []


# ---------------------------------------------------------------------------
# AC8 — UNKNOWN → NORMAL transition on fresh data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac8_unknown_to_normal_requires_fresh_data():
    """Transition from UNKNOWN to NORMAL happens when fresh data arrives."""
    call_count = 0

    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("temporary outage")
        return _make_vix_df(15.0)

    dm = SimpleNamespace(history=_history)
    agg = MarketRiskAggregator(dm)

    with patch("yfinance.download", side_effect=RuntimeError("down")):
        state1, _, _, _, _ = await agg.assess()
        assert state1 is MarketRiskState.UNKNOWN

        state2, _, _, _, _ = await agg.assess()
        assert state2 is MarketRiskState.NORMAL


# ---------------------------------------------------------------------------
# AC9 — Determinism
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac9_identical_inputs_identical_state():
    agg = _aggregator_with_mock(vix_value=15.0)
    T = datetime(2025, 6, 1, 15, 0, tzinfo=UTC)
    r1 = await agg.assess(as_of=T)
    r2 = await agg.assess(as_of=T)
    assert r1[0] is r2[0]
    assert r1[2] == r2[2]


# ---------------------------------------------------------------------------
# AC10 — as_of bounds fetches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac10_as_of_bounds_history_fetches():
    T = datetime(2025, 3, 15, 14, 30, tzinfo=UTC)
    calls = []

    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        calls.append((symbol, timeframe, start, end))
        return _make_vix_df(15.0)

    dm = SimpleNamespace(history=_history)
    agg = MarketRiskAggregator(dm)
    await agg.assess(as_of=T)

    for _, _, _, end in calls:
        assert end <= T + timedelta(seconds=1)


# ---------------------------------------------------------------------------
# AC11-14 — scanner.run() routing
# ---------------------------------------------------------------------------

def _make_scanner_with_state(forced_state: MarketRiskState) -> MarketScanner:
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        return pd.DataFrame()

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])

    async def _fake_assess(as_of=None):
        return forced_state, 1.0, [f"forced_{forced_state.value}"]

    scanner._assess_market_risk = _fake_assess
    return scanner


@pytest.mark.asyncio
async def test_ac11_normal_proceeds_to_pipeline():
    scanner = _make_scanner_with_state(MarketRiskState.NORMAL)
    outcome, watchlist = await scanner.run()
    assert outcome in (ScannerOutcome.SUCCESS_EMPTY, ScannerOutcome.SUCCESS_NONEMPTY)


@pytest.mark.asyncio
async def test_ac12_caution_proceeds_to_pipeline():
    scanner = _make_scanner_with_state(MarketRiskState.CAUTION)
    outcome, watchlist = await scanner.run()
    assert outcome in (ScannerOutcome.SUCCESS_EMPTY, ScannerOutcome.SUCCESS_NONEMPTY)


@pytest.mark.asyncio
async def test_ac13_stress_returns_defensive_watchlist():
    from app.core.scanner import DEFENSIVE_WATCHLIST
    scanner = _make_scanner_with_state(MarketRiskState.STRESS)
    outcome, watchlist = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_NONEMPTY
    assert watchlist == DEFENSIVE_WATCHLIST


@pytest.mark.asyncio
async def test_ac14_panic_returns_defensive_watchlist():
    from app.core.scanner import DEFENSIVE_WATCHLIST
    scanner = _make_scanner_with_state(MarketRiskState.PANIC)
    outcome, watchlist = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_NONEMPTY
    assert watchlist == DEFENSIVE_WATCHLIST


# ---------------------------------------------------------------------------
# 046 regressions — check_spy_volatility backward compat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_046_regression_spy_exception_fails_closed():
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        raise ConnectionError("down")

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])
    with patch("yfinance.download", side_effect=RuntimeError("down")):
        result = await scanner.check_spy_volatility()
        assert result is SpyVolatilityState.UNKNOWN


@pytest.mark.asyncio
async def test_046_regression_panic_atr_returns_panic():
    agg = _aggregator_with_mock(vix_value=35.0)
    scanner = MarketScanner(SimpleNamespace(provider=SimpleNamespace(supports_asset_discovery=False)), [])
    scanner._regime = agg
    result = await scanner.check_spy_volatility()
    assert result is SpyVolatilityState.PANIC


@pytest.mark.asyncio
async def test_046_regression_normal_atr_returns_safe():
    agg = _aggregator_with_mock(vix_value=15.0)
    scanner = MarketScanner(SimpleNamespace(provider=SimpleNamespace(supports_asset_discovery=False)), [])
    scanner._regime = agg
    result = await scanner.check_spy_volatility()
    assert result is SpyVolatilityState.SAFE
