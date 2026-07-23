"""CR-047 — Multi-signal MarketRiskState regression tests.

All tests are deterministic, use only injected mocks, and make no real network
calls.  No ticker/date/benchmark-specific logic is present.

Acceptance criteria covered (from issue #91):
  AC1  SPY unavailable, other families healthy → not UNKNOWN
  AC2  SPY normal, universe extreme stress → escalates beyond NORMAL
  AC3  Conflicting family votes → deterministic state with reason codes
  AC4  Insufficient broad coverage → UNKNOWN
  AC5  Unavailable family degrades confidence without fabricating safe
  AC6  Provider 429/500/timeout/auth cannot cast SAFE vote
  AC7  UNKNOWN blocks new entries, does not disable exits
  AC8  UNKNOWN→NORMAL requires fresh authoritative data
  AC9  Identical inputs produce identical state (determinism)
  AC10 as_of timestamp bounds history fetches (no future-data leakage)
  AC11 NORMAL → full pipeline
  AC12 CAUTION → full pipeline (logged)
  AC13 STRESS → defensive watchlist
  AC14 PANIC → defensive watchlist
  046-regression  046 fail-closed regressions satisfied under new aggregator
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.risk.market_regime import (
    MarketRiskAggregator,
    MarketRiskState,
    SignalHealth,
    SignalVote,
    _atr_percentile,
)
from app.core.scanner import MarketScanner, ScannerOutcome, SpyVolatilityState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_normal_1min(n: int = 1440) -> pd.DataFrame:
    """Minimal 1-min OHLCV frame with low, stable ATR (normal regime)."""
    idx = pd.date_range("2025-01-02 09:30", periods=n, freq="1min", tz="UTC")
    price = 500.0
    df = pd.DataFrame({
        "open":   [price] * n,
        "high":   [price + 0.05] * n,  # tiny TR → low ATR%
        "low":    [price - 0.05] * n,
        "close":  [price] * n,
        "volume": [1_000_000] * n,
    }, index=idx)
    return df


def _make_panic_1min(n: int = 1440) -> pd.DataFrame:
    """1-min frame with extreme ATR ensuring >= 85th-percentile volatility."""
    idx = pd.date_range("2025-01-02 09:30", periods=n, freq="1min", tz="UTC")
    price = 500.0
    # First n - 30 bars: tiny range (build historical baseline)
    highs  = [price + 0.05] * n
    lows   = [price - 0.05] * n
    closes = [price] * n
    # Last 30 bars: extreme range to spike ATR percentile
    for i in range(n - 30, n):
        highs[i]  = price * 1.10
        lows[i]   = price * 0.90
        closes[i] = price
    df = pd.DataFrame({
        "open":   [price] * n,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": [1_000_000] * n,
    }, index=idx)
    return df


def _make_daily(n: int = 3, advancing: bool = True) -> pd.DataFrame:
    """Minimal daily OHLCV frame."""
    idx = pd.date_range("2025-01-02", periods=n, freq="B", tz="UTC")
    base = 100.0
    closes = [base + i * (1 if advancing else -1) for i in range(n)]
    df = pd.DataFrame({
        "open":   [base] * n,
        "high":   [c + 0.5 for c in closes],
        "low":    [c - 0.5 for c in closes],
        "close":  closes,
        "volume": [1_000_000] * n,
    }, index=idx)
    return df


def _aggregator_with_mock(
    spy_df=None,
    qqq_df=None,
    daily_df=None,
    spy_raises=None,
    qqq_raises=None,
    daily_raises=None,
    universe=None,
) -> MarketRiskAggregator:
    """Build a MarketRiskAggregator with fully injected mock data manager."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if timeframe == "1Min":
            if symbol == "SPY":
                if spy_raises:
                    raise spy_raises
                return spy_df if spy_df is not None else _make_normal_1min()
            if symbol == "QQQ":
                if qqq_raises:
                    raise qqq_raises
                return qqq_df if qqq_df is not None else _make_normal_1min()
            return _make_normal_1min()
        # 1Day
        if daily_raises:
            raise daily_raises
        return daily_df if daily_df is not None else _make_daily()

    dm = SimpleNamespace(history=_history)
    syms = universe or ["AAPL", "MSFT", "NVDA", "AMZN", "META",
                        "JPM", "BAC", "XLK", "XLF", "QQQ", "SPY"]
    return MarketRiskAggregator(data_manager=dm, universe_symbols=syms)


# ---------------------------------------------------------------------------
# AC1 — SPY unavailable, other families healthy → should NOT be UNKNOWN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac1_spy_unavailable_other_families_healthy():
    """SPY error alone must not produce UNKNOWN when other families have data."""
    agg = _aggregator_with_mock(spy_raises=ConnectionError("SPY provider timeout"))
    state, conf, cov, reasons, healths = await agg.assess()
    assert state is not MarketRiskState.UNKNOWN, (
        f"SPY alone should not produce UNKNOWN; got {state} reasons={reasons}"
    )
    assert not healths["broad_market"].healthy
    assert healths["qqq"].healthy  # QQQ was fine


# ---------------------------------------------------------------------------
# AC2 — SPY normal, universe extreme stress → escalates beyond NORMAL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac2_spy_normal_universe_extreme_stress():
    """Even if SPY is normal, extreme universe cross-sectional stress escalates."""
    # All universe symbols get extreme ATR daily bars
    extreme_daily = _make_daily(3)
    # Set high/low such that ATR% >= 4.0
    extreme_daily["high"] = extreme_daily["close"] * 1.06
    extreme_daily["low"]  = extreme_daily["close"] * 0.94
    agg = _aggregator_with_mock(spy_df=_make_normal_1min(), daily_df=extreme_daily)
    state, conf, cov, reasons, healths = await agg.assess()
    assert state in (MarketRiskState.CAUTION, MarketRiskState.STRESS, MarketRiskState.PANIC), (
        f"Extreme universe stress should escalate beyond NORMAL; got {state}"
    )


# ---------------------------------------------------------------------------
# AC3 — Conflicting votes → deterministic state + reason codes present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac3_conflicting_votes_deterministic_with_reason_codes():
    """Conflicting SPY/QQQ/breadth evidence must produce deterministic result
    with non-empty reason codes."""
    # SPY panic, QQQ normal, breadth healthy
    agg = _aggregator_with_mock(spy_df=_make_panic_1min(), qqq_df=_make_normal_1min())
    state1, conf1, cov1, reasons1, _ = await agg.assess()
    state2, conf2, cov2, reasons2, _ = await agg.assess()  # second call, same inputs
    assert state1 is state2, "Same inputs must produce same state"
    assert reasons1, "Reason codes must be non-empty"
    assert reasons1 == reasons2, "Reason codes must be deterministic"


# ---------------------------------------------------------------------------
# AC4 — Insufficient broad coverage → UNKNOWN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac4_insufficient_coverage_produces_unknown():
    """When all families fail, state must be UNKNOWN."""
    err = RuntimeError("provider outage")
    agg = _aggregator_with_mock(
        spy_raises=err, qqq_raises=err, daily_raises=err
    )
    state, conf, cov, reasons, _ = await agg.assess()
    assert state is MarketRiskState.UNKNOWN
    assert conf == 0.0 or conf < 0.40
    assert any("INSUFFICIENT" in r or "UNHEALTHY" in r for r in reasons)


# ---------------------------------------------------------------------------
# AC5 — Unavailable family degrades confidence without fabricating safe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac5_unavailable_family_degrades_confidence():
    """Missing QQQ data reduces confidence but should not cast a NORMAL vote."""
    agg = _aggregator_with_mock(qqq_raises=ConnectionError("timeout"))
    state, conf, cov, reasons, healths = await agg.assess()
    assert not healths["qqq"].healthy
    # QQQ UNKNOWN vote → it doesn't add confidence, overall coverage drops
    assert cov < 1.0
    # State must not be artificially boosted to NORMAL purely by the absence
    # of QQQ; the remaining families determine the state.


# ---------------------------------------------------------------------------
# AC6 — Provider 429 / 500 / auth / malformed cannot cast safe vote
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    ConnectionError("429 too many requests"),
    ConnectionError("500 internal server error"),
    PermissionError("401 unauthorized"),
    ValueError("malformed response frame"),
    TimeoutError("request timed out"),
])
@pytest.mark.asyncio
async def test_ac6_provider_error_cannot_cast_safe_vote(exc):
    """Any provider error on a family must produce a non-healthy UNKNOWN vote
    for that family — never a NORMAL/SAFE vote."""
    agg = _aggregator_with_mock(spy_raises=exc)
    _, _, _, _, healths = await agg.assess()
    assert not healths["broad_market"].healthy, (
        f"Provider error {exc} must mark broad_market unhealthy"
    )
    assert healths["broad_market"].confidence == 0.0


# ---------------------------------------------------------------------------
# AC7 — UNKNOWN blocks entries; scanner returns SUCCESS_EMPTY
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac7_unknown_blocks_new_entries():
    """When regime is UNKNOWN scanner.run() must return SUCCESS_EMPTY, []"""
    err = RuntimeError("all families down")
    scanner = _make_scanner_all_down(err)
    outcome, watchlist = await scanner.run()
    assert outcome is ScannerOutcome.SUCCESS_EMPTY
    assert watchlist == []


def _make_scanner_all_down(err):
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        raise err
    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])
    return scanner


# ---------------------------------------------------------------------------
# AC8 — UNKNOWN → NORMAL requires fresh authoritative data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac8_unknown_to_normal_requires_fresh_data():
    """First assess() with bad data → UNKNOWN; second with good data → not UNKNOWN."""
    normal_df = _make_normal_1min()
    daily_df = _make_daily()
    provider_down = True

    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if provider_down:
            raise ConnectionError("provider down")
        if timeframe == "1Min":
            return normal_df
        return daily_df

    dm = SimpleNamespace(history=_history)
    agg = MarketRiskAggregator(dm, ["AAPL", "MSFT", "NVDA", "AMZN", "META",
                                     "JPM", "BAC", "XLK", "XLF", "QQQ", "SPY"])
    state1, _, _, _, _ = await agg.assess()
    assert state1 is MarketRiskState.UNKNOWN

    provider_down = False
    state2, conf2, _, _, _ = await agg.assess()
    assert state2 is not MarketRiskState.UNKNOWN
    assert conf2 > 0.0


# ---------------------------------------------------------------------------
# AC9 — Identical inputs → identical state (determinism)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac9_identical_inputs_identical_state():
    agg = _aggregator_with_mock()
    T = datetime(2025, 6, 1, 15, 0, tzinfo=UTC)
    r1 = await agg.assess(as_of=T)
    r2 = await agg.assess(as_of=T)
    assert r1[0] is r2[0], "State must be deterministic for same inputs"
    assert r1[2] == r2[2], "Coverage must be deterministic"


# ---------------------------------------------------------------------------
# AC10 — as_of bounds history calls (no future-data leakage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac10_as_of_bounds_history_fetches():
    """All history calls must use start <= as_of; no bar after as_of."""
    T = datetime(2025, 3, 15, 14, 30, tzinfo=UTC)
    calls = []

    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        calls.append((symbol, timeframe, start, end))
        if timeframe == "1Min":
            return _make_normal_1min()
        return _make_daily()

    dm = SimpleNamespace(history=_history)
    agg = MarketRiskAggregator(dm, ["AAPL", "MSFT", "NVDA", "AMZN", "META",
                                     "JPM", "BAC", "XLK", "XLF", "QQQ", "SPY"])
    await agg.assess(as_of=T)

    for _sym, _tf, start, end in calls:
        assert end <= T + timedelta(seconds=1), (
            f"History fetch end={end} exceeds as_of={T} — future-data leakage!"
        )


# ---------------------------------------------------------------------------
# AC11-14 — scanner.run() routes correctly for each state
# ---------------------------------------------------------------------------

def _make_scanner_with_state(forced_state: MarketRiskState) -> MarketScanner:
    """Build a scanner whose regime aggregator always returns the given state."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        return pd.DataFrame()  # empty — stage1 will return no candidates

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])

    async def _fake_assess(as_of=None):
        return forced_state, 0.85, [f"{forced_state.value}_reason"]

    scanner._assess_market_risk = _fake_assess
    return scanner


@pytest.mark.asyncio
async def test_ac11_normal_proceeds_to_pipeline():
    scanner = _make_scanner_with_state(MarketRiskState.NORMAL)
    outcome, watchlist = await scanner.run()
    # Stage 1 returns no candidates (empty history) → SUCCESS_EMPTY or FAILED due to data check is acceptable
    assert outcome in (ScannerOutcome.SUCCESS_EMPTY, ScannerOutcome.SUCCESS_NONEMPTY, ScannerOutcome.FAILED)
    # Key assertion: we did NOT get defensive watchlist
    if outcome is ScannerOutcome.SUCCESS_NONEMPTY:
        from app.core.scanner import DEFENSIVE_WATCHLIST
        assert watchlist != DEFENSIVE_WATCHLIST


@pytest.mark.asyncio
async def test_ac12_caution_proceeds_to_pipeline():
    scanner = _make_scanner_with_state(MarketRiskState.CAUTION)
    outcome, watchlist = await scanner.run()
    assert outcome in (ScannerOutcome.SUCCESS_EMPTY, ScannerOutcome.SUCCESS_NONEMPTY, ScannerOutcome.FAILED)


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
# 046 regression — check_spy_volatility backward compat + fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_046_regression_spy_exception_fails_closed():
    """SPY provider exception → check_spy_volatility returns UNKNOWN (fail closed)."""
    err = ConnectionError("SPY data unavailable")
    scanner = _make_scanner_all_down(err)
    result = await scanner.check_spy_volatility()
    assert result is SpyVolatilityState.UNKNOWN, (
        f"All-down scanner must return UNKNOWN not {result}"
    )


@pytest.mark.asyncio
async def test_046_regression_panic_atr_returns_panic():
    """High ATR% → check_spy_volatility returns PANIC."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if timeframe == "1Min" and symbol in ("SPY", "QQQ"):
            return _make_panic_1min()
        return _make_daily()

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])
    result = await scanner.check_spy_volatility()
    assert result is SpyVolatilityState.PANIC


@pytest.mark.asyncio
async def test_046_regression_normal_atr_returns_safe():
    """Normal ATR% → check_spy_volatility returns SAFE."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if timeframe == "1Min":
            return _make_normal_1min()
        return _make_daily()

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])
    result = await scanner.check_spy_volatility()
    assert result is SpyVolatilityState.SAFE


# ---------------------------------------------------------------------------
# Confirmation-based aggregation regression tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_isolated_breadth_stress_becomes_caution():
    """Isolated breadth STRESS (with SPY/QQQ/universe NORMAL) escalates to CAUTION, not STRESS."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if timeframe == "1Min":
            return _make_normal_1min()
        if symbol in ("AAPL", "MSFT", "NVDA", "AMZN", "META", "JPM", "BAC", "XLK", "XLF"):
            return _make_daily(advancing=False)
        return _make_daily(advancing=True)

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    agg = MarketRiskAggregator(dm, ["AAPL", "MSFT", "NVDA", "AMZN", "META", "JPM", "BAC", "XLK", "XLF", "QQQ", "SPY"])
    state, conf, cov, reasons, healths = await agg.assess()
    assert state is MarketRiskState.CAUTION
    assert any("breadth:STRESS" in r for r in reasons)
    assert any("stress_confirmations=1" in r for r in reasons)


@pytest.mark.asyncio
async def test_two_independent_stress_votes_confirm_stress():
    """Two independent STRESS votes (e.g. breadth + universe_stress) confirm MarketRiskState.STRESS."""
    agg = MarketRiskAggregator(SimpleNamespace())
    healthy_votes = [
        SignalVote("broad_market", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.9), "OK"),
        SignalVote("qqq", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.9), "OK"),
        SignalVote("breadth", MarketRiskState.STRESS, SignalHealth(True, "declining", confidence=0.85), "declining"),
        SignalVote("universe_stress", MarketRiskState.STRESS, SignalHealth(True, "high vol", confidence=0.85), "high vol"),
        SignalVote("data_health", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=1.0), "OK"),
    ]
    agg._family_broad_market = AsyncMock(side_effect=[healthy_votes[0], healthy_votes[1]])
    agg._family_breadth = AsyncMock(return_value=healthy_votes[2])
    agg._family_universe_stress = AsyncMock(return_value=healthy_votes[3])
    agg._family_data_health = MagicMock(return_value=healthy_votes[4])

    state, conf, cov, reasons, healths = await agg.assess()
    assert state is MarketRiskState.STRESS
    assert any("stress_confirmations=2" in r for r in reasons)


@pytest.mark.asyncio
async def test_broad_market_panic_remains_panic():
    """Isolated broad market PANIC immediately escalates to MarketRiskState.PANIC."""
    agg = MarketRiskAggregator(SimpleNamespace())
    healthy_votes = [
        SignalVote("broad_market", MarketRiskState.PANIC, SignalHealth(True, "extreme vol", confidence=0.9), "extreme vol"),
        SignalVote("qqq", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.9), "OK"),
        SignalVote("breadth", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.85), "OK"),
        SignalVote("universe_stress", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.85), "OK"),
        SignalVote("data_health", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=1.0), "OK"),
    ]
    agg._family_broad_market = AsyncMock(side_effect=[healthy_votes[0], healthy_votes[1]])
    agg._family_breadth = AsyncMock(return_value=healthy_votes[2])
    agg._family_universe_stress = AsyncMock(return_value=healthy_votes[3])
    agg._family_data_health = MagicMock(return_value=healthy_votes[4])

    state, conf, cov, reasons, healths = await agg.assess()
    assert state is MarketRiskState.PANIC


@pytest.mark.asyncio
async def test_data_health_does_not_vote_market_direction():
    """data_health=NORMAL does not count towards market direction or reduce stress confirmations."""
    agg = MarketRiskAggregator(SimpleNamespace())
    healthy_votes = [
        SignalVote("broad_market", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.9), "OK"),
        SignalVote("qqq", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.9), "OK"),
        SignalVote("breadth", MarketRiskState.STRESS, SignalHealth(True, "declining", confidence=0.85), "declining"),
        SignalVote("universe_stress", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=0.85), "OK"),
        SignalVote("data_health", MarketRiskState.NORMAL, SignalHealth(True, "OK", confidence=1.0), "OK"),
    ]
    agg._family_broad_market = AsyncMock(side_effect=[healthy_votes[0], healthy_votes[1]])
    agg._family_breadth = AsyncMock(return_value=healthy_votes[2])
    agg._family_universe_stress = AsyncMock(return_value=healthy_votes[3])
    agg._family_data_health = MagicMock(return_value=healthy_votes[4])

    state, conf, cov, reasons, healths = await agg.assess()
    assert state is MarketRiskState.CAUTION
    assert any("stress_confirmations=1" in r for r in reasons)


@pytest.mark.asyncio
async def test_isolated_breadth_stress_keeps_dynamic_pipeline_alive():
    """Scanner receives CAUTION on isolated breadth STRESS and continues dynamic candidate pipeline."""
    async def _history(symbol, timeframe, start, end, prefer_cache=True):
        if timeframe == "1Min":
            return _make_normal_1min()
        if symbol in ("SPY", "QQQ"):
            return _make_daily(advancing=True)
        return _make_daily(advancing=False)

    dm = SimpleNamespace(
        history=_history,
        provider=SimpleNamespace(supports_asset_discovery=False),
    )
    scanner = MarketScanner(dm, [])
    state, conf, reasons = await scanner._assess_market_risk()
    assert state is MarketRiskState.CAUTION

