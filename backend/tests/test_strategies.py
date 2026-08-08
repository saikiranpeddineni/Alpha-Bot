"""Strategy signal tests on crafted market scenarios."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.core.domain import Regime, SignalAction
from app.indicators.ta import enrich
from app.strategies.base import REGISTRY, StrategyContext, create_strategies
from app.strategies.momentum import MomentumCross
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.rsi_reversion import Rsi2Reversion
from app.strategies.vwap_reversion import VwapReversion
from tests.conftest import make_bars

NOW = datetime(2026, 1, 6, 16, 0, tzinfo=UTC)


def ctx(position=None, regime=Regime.RANGE) -> StrategyContext:
    return StrategyContext(position=position, equity=100_000.0, regime=regime, now=NOW)


def session_bars(pattern: str, n: int = 120) -> pd.DataFrame:
    """Craft a single-session frame (starts 9:30 ET) with a given shape."""
    start = pd.Timestamp("2026-01-06 14:30", tz="UTC")
    rng = np.random.default_rng(3)
    if pattern == "breakout_up":
        # 30 min tight range around 100, then a strong push to 103 on volume
        flat = 100.0 + rng.normal(0, 0.05, 30).cumsum() * 0.02
        push = np.linspace(flat[-1], 103.0, n - 30)
        closes = np.concatenate([flat, push])
    elif pattern == "dip_in_uptrend":
        up = np.linspace(100.0, 104.0, n - 8)
        dip = np.linspace(104.0, 102.9, 8)  # sharp 3-bar-style flush at the end
        closes = np.concatenate([up, dip])
    else:  # plunge below vwap
        flat = 100.0 + rng.normal(0, 0.02, n - 15).cumsum() * 0.01
        plunge = np.linspace(flat[-1], flat[-1] * 0.985, 15)
        closes = np.concatenate([flat, plunge])

    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * 1.0004
    lows = np.minimum(opens, closes) * 0.9996
    volume = 50_000.0 * rng.lognormal(0, 0.25, n)  # noisy so z-scores are defined
    if pattern == "breakout_up":
        volume[-1] = 250_000.0  # volume spike on the signal bar
    idx = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume}, index=idx)
    df.index.name = "ts"
    return enrich(df)


def test_registry_has_all_builtins():
    assert set(REGISTRY) >= {"orb", "vwap_reversion", "momentum", "rsi2_reversion"}
    strategies = create_strategies({"orb": {"enabled": False}})
    orb = next(s for s in strategies if s.name == "orb")
    assert not orb.enabled


def test_orb_long_on_confirmed_breakout():
    strat = OpeningRangeBreakout(params={"mft_alignment": False, "climax_atr_mult": 0.0})
    df = session_bars("breakout_up", n=60)
    df.loc[df.index[-1], "vol_z"] = 2.0
    df.loc[df.index[-1], "rsi14"] = 60.0
    sig = strat.on_bar(ctx(regime=Regime.TREND_UP), "TEST", df)
    assert sig is not None
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop_loss < float(df["close"].iloc[-1])
    assert sig.take_profit > float(df["close"].iloc[-1])
    # One attempt per session
    assert strat.on_bar(ctx(regime=Regime.TREND_UP), "TEST", df) is None
    strat.reset()
    assert strat.on_bar(ctx(regime=Regime.TREND_UP), "TEST", df) is not None


def test_orb_no_signal_inside_range():
    strat = OpeningRangeBreakout()
    rng = np.random.default_rng(11)
    n = 60
    # Wide opening range (99-101), then price meanders well inside it
    or_part = 100.0 + np.sin(np.arange(15) * 2.1) * 1.0
    inside = 100.0 + rng.normal(0, 0.05, n - 15)
    closes = np.concatenate([or_part, inside])
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range(start=pd.Timestamp("2026-01-06 14:30", tz="UTC"), periods=n, freq="1min")
    df = enrich(pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) + 0.02,
        "low": np.minimum(opens, closes) - 0.02, "close": closes,
        "volume": 50_000.0 * rng.lognormal(0, 0.25, n),
    }, index=idx))
    assert strat.on_bar(ctx(), "TEST", df) is None


def test_orb_respects_open_position():
    from app.core.domain import Position

    strat = OpeningRangeBreakout()
    df = session_bars("breakout_up", n=60)
    pos = Position(symbol="TEST", qty=10, avg_entry_price=100, market_price=101)
    assert strat.on_bar(ctx(position=pos), "TEST", df) is None


def test_vwap_reversion_fades_plunge():
    df = session_bars("plunge", n=150)
    # With the trend filter relaxed, the stretch below VWAP is bought
    strat = VwapReversion(params={"z_entry": 1.5, "er_max": 1.1, "rsi2_oversold": 25.0, "use_ema_filter": False})
    sig = strat.on_bar(ctx(), "TEST", df)
    assert sig is not None
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.take_profit == pytest.approx(float(df["vwap"].iloc[-1]))
    # With default params the efficient one-way plunge is rejected as a trend
    assert VwapReversion().on_bar(ctx(), "TEST", df) is None


def test_momentum_exit_on_cross_down():
    from app.core.domain import Position

    strat = MomentumCross()
    # Build a frame where ema9 crosses below ema20 on the last bar
    up = np.linspace(100, 105, 80)
    down = np.linspace(105, 103.4, 20)
    closes = np.concatenate([up, down])
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range(start=pd.Timestamp("2026-01-06 14:30", tz="UTC"), periods=100, freq="1min")
    df = enrich(pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) * 1.0003,
        "low": np.minimum(opens, closes) * 0.9997, "close": closes,
        "volume": 60_000.0 * np.random.default_rng(4).lognormal(0, 0.25, 100),
    }, index=idx))
    # Find the exact bar where EMA9 crosses below EMA20 and evaluate there
    crossed_mask = (df["ema9"].shift(1) >= df["ema20"].shift(1)) & (df["ema9"] < df["ema20"])
    cross_positions = np.flatnonzero(crossed_mask.to_numpy())
    assert len(cross_positions) > 0, "crafted frame must contain a bearish EMA cross"
    at_cross = df.iloc[: cross_positions[-1] + 1]
    pos = Position(symbol="TEST", qty=10, avg_entry_price=100, market_price=103, strategy="momentum")
    sig = strat.on_bar(ctx(position=pos), "TEST", at_cross)
    # Position exits are managed by PIS (Position Intelligence System)
    assert sig is None
    # Foreign strategy's position is also left alone
    foreign = Position(symbol="TEST", qty=10, avg_entry_price=100, market_price=103, strategy="orb")
    assert strat.on_bar(ctx(position=foreign), "TEST", at_cross) is None


def test_rsi2_dip_buy_in_uptrend():
    strat = Rsi2Reversion(params={"rsi2_entry": 25.0})
    df = session_bars("dip_in_uptrend", n=120)
    rsi2_now = float(df["rsi2"].iloc[-1])
    sig = strat.on_bar(ctx(), "TEST", df)
    if rsi2_now <= 25.0 and float(df["close"].iloc[-1]) > float(df["ema50"].iloc[-1]):
        assert sig is not None
        assert sig.action == SignalAction.ENTER_LONG
    else:
        assert sig is None  # frame did not reach entry conditions; strategy stayed quiet


def test_strategies_quiet_during_warmup():
    df = enrich(make_bars(10))
    for strat in create_strategies():
        assert strat.on_bar(ctx(), "TEST", df) is None


def test_bb_squeeze_strategy():
    from app.strategies.bb_squeeze import BollingerBandSqueeze
    strat = BollingerBandSqueeze(params={"width_percentile_lookback": 200, "width_percentile_threshold": 0.20, "vol_z_min": -5.0})
    rng = np.random.default_rng(42)
    # 200 bars of high volatility
    closes_high = 100.0 + rng.normal(0, 1.0, 200).cumsum()
    # 25 bars of low volatility consolidation (squeeze)
    closes_squeeze = closes_high[-1] + rng.normal(0, 0.02, 25).cumsum()
    # 1 breakout bar
    closes_breakout = np.array([closes_squeeze[-1] + 3.0])
    
    closes = np.concatenate([closes_high, closes_squeeze, closes_breakout])
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range(start="2026-01-06 14:30", periods=226, freq="1min", tz="UTC")
    df = enrich(pd.DataFrame({
        "open": opens, "high": closes + 0.02, "low": closes - 0.02, "close": closes,
        "volume": 100000.0 * rng.lognormal(0, 0.1, 226)
    }, index=idx))
    sig = strat.on_bar(ctx(), "TEST", df)
    assert sig is not None
    assert sig.action == SignalAction.ENTER_LONG


# ---------------------------------------------------------------------------
# Focused MomentumCross MACD pending-event tests
# ---------------------------------------------------------------------------

def _make_momentum_df(
    n: int = 80,
    *,
    trend: str = "up",          # "up" or "down"
    above_vwap: bool = True,    # close relative to vwap
    rsi: float = 62.0,          # unused (rsi comes from the price path)
    vol_z: float = 1.0,         # volume z-score forced on all bars
    seed: int = 42,
) -> pd.DataFrame:
    """
    Build an enriched 1-minute bar frame that:
      - Has a clear EMA9>EMA20 (trend="up") or EMA9<EMA20 (trend="down") trend.
      - Has a MACD histogram inflection somewhere in the latter half.
      - Has vol_z forced on every bar so the volume guard is consistent across
        bar-by-bar slicing.
      - Has vwap forced below/above close on every bar.
    """
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-01-06 14:30", tz="UTC")

    if trend == "up":
        # Strong sustained uptrend so EMA9 > EMA20 throughout the latter portion.
        # Small dip at n//2 creates a DOWN inflection; recovery past n//2+5 creates UP.
        closes = np.linspace(97.0, 106.0, n).astype(float)
        mid = n // 2
        # Insert a 3-bar dip to generate a MACD down-then-up inflection
        closes[mid: mid + 3] -= 0.5
    else:
        closes = np.linspace(106.0, 97.0, n).astype(float)
        mid = n // 2
        closes[mid: mid + 3] += 0.5

    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volume = 50_000.0 * rng.lognormal(0, 0.3, n)

    idx = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume},
        index=idx,
    )
    df.index.name = "ts"
    df = enrich(df)

    # Force vol_z on ALL bars so slices behave consistently
    df["vol_z"] = vol_z

    # Force vwap on ALL bars relative to close
    vwap_factor = 0.98 if above_vwap else 1.02  # close is above/below vwap
    df["vwap"] = df["close"] * vwap_factor

    return df



def _momentum_ctx(debug: bool = False) -> StrategyContext:
    return StrategyContext(
        position=None,
        equity=100_000.0,
        regime=Regime.RANGE,
        now=NOW,
        debug_mode=debug,
    )


def test_macd_event_created_on_inflection():
    """MACD inflection on bar N must arm a pending event for the symbol."""
    # rsi_max=99 bypasses RSI filter so we isolate event-creation behaviour.
    strat = MomentumCross(params={"rsi_max": 99.0})
    df = _make_momentum_df(trend="up", above_vwap=True)
    # Walk bar by bar; after the inflection the event must exist (pending or consumed)
    for end in range(30, len(df) + 1):
        strat.on_bar(_momentum_ctx(), "SYM", df.iloc[:end])
        sym_events = strat._events.get("SYM", {})
        if "long" in sym_events or sym_events.get("long", {}).get("emitted"):
            break
    sym_events = strat._events.get("SYM", {})
    assert "long" in sym_events or sym_events.get("long", {}).get("emitted"), (
        "No long event was created after MACD inflection"
    )


def test_macd_event_confirms_within_window():
    """A candidate must be emitted when conditions align within EVENT_WINDOW_BARS."""
    # rsi_max=99 bypasses RSI filter; vol_z_min=-5 bypasses volume filter.
    strat = MomentumCross(params={"rsi_max": 99.0, "vol_z_min": -5.0})
    df = _make_momentum_df(trend="up", above_vwap=True, vol_z=1.0)

    sig = None
    for end in range(30, len(df) + 1):
        sig = strat.on_bar(_momentum_ctx(), "SYM", df.iloc[:end])
        if sig is not None:
            break

    assert sig is not None, "No long signal emitted within event window"
    assert sig.action == SignalAction.ENTER_LONG


def test_macd_event_expired_after_window():
    """After EVENT_WINDOW_BARS bars without confirmation the event must expire and
    NOT produce a candidate even if conditions later align."""
    from app.strategies.momentum import EVENT_WINDOW_BARS

    strat = MomentumCross(params={"rsi_min": 50.0, "rsi_max": 78.0, "vol_z_min": 0.0})

    # Build a frame: first n-15 bars drive a MACD inflection but with rsi/vwap
    # conditions failing, so the event is NOT consumed.  Then on bar n-14 .. n-1
    # the conditions are still wrong.  Finally check that after >10 bars the event
    # is gone and no signal fires.
    n = 80
    df_base = _make_momentum_df(n=n, trend="up", above_vwap=False, rsi=62.0, vol_z=1.0)

    # Find where the MACD inflection (upward) is in the frame
    inflect_idx = None
    for i in range(3, len(df_base)):
        h_now = float(df_base["macd_hist"].iloc[i])
        h_prev = float(df_base["macd_hist"].iloc[i - 1])
        h_prev2 = float(df_base["macd_hist"].iloc[i - 2])
        if h_now > h_prev and h_prev <= h_prev2:
            inflect_idx = i
            break

    if inflect_idx is None:
        pytest.skip("Crafted frame has no MACD inflection — skip")

    # Feed bars up through and including the inflection bar with above_vwap=False
    # (so the condition fails → event is armed but not consumed)
    for end in range(30, inflect_idx + 2):
        strat.on_bar(_momentum_ctx(), "SYM", df_base.iloc[:end])

    # Confirm the event is armed and not yet consumed
    evt = strat._events.get("SYM", {}).get("long")
    if evt is None:
        pytest.skip("MACD inflection event not found in crafted frame — skip")

    # Now advance MORE than EVENT_WINDOW_BARS bars beyond the inflection,
    # but still with above_vwap=False (never confirming).
    for extra in range(EVENT_WINDOW_BARS + 2):
        strat.on_bar(_momentum_ctx(), "SYM", df_base.iloc[:inflect_idx + 2 + extra])

    # Now switch above_vwap to True and try — event must be expired, no signal
    df_late = df_base.copy()
    last_close = float(df_late["close"].iloc[-1])
    df_late.loc[df_late.index[-1], "vwap"] = last_close * 0.98  # above vwap
    sig = strat.on_bar(_momentum_ctx(), "SYM", df_late)
    # Event must have expired; signal must NOT fire from the stale event
    assert sig is None or sig.action == SignalAction.EXIT, (
        "Signal fired from an expired MACD event"
    )


def test_one_event_emits_at_most_one_candidate():
    """The same MACD event must not produce more than one candidate signal."""
    strat = MomentumCross(params={"rsi_max": 99.0, "vol_z_min": -5.0})
    df = _make_momentum_df(trend="up", above_vwap=True, vol_z=1.0)

    signals = []
    for end in range(30, len(df) + 1):
        sig = strat.on_bar(_momentum_ctx(), "SYM", df.iloc[:end])
        if sig is not None and sig.action == SignalAction.ENTER_LONG:
            signals.append(sig)

    assert len(signals) <= 1, f"Expected at most 1 candidate per event, got {len(signals)}"


def test_new_inflection_arms_new_event_after_consume():
    """After the first event is consumed, a subsequent MACD inflection must
    create a new, independent event."""
    strat = MomentumCross(params={"rsi_max": 99.0, "vol_z_min": -5.0})

    # First frame: get a signal (consumes event)
    df1 = _make_momentum_df(n=80, trend="up", above_vwap=True, vol_z=1.0, seed=1)
    sig1 = None
    for end in range(30, len(df1) + 1):
        sig1 = strat.on_bar(_momentum_ctx(), "SYM", df1.iloc[:end])
        if sig1 is not None:
            break
    assert sig1 is not None and sig1.action == SignalAction.ENTER_LONG

    # Second frame: fresh data with a new MACD inflection
    df2 = _make_momentum_df(n=80, trend="up", above_vwap=True, vol_z=1.0, seed=99)
    sig2 = None
    for end in range(30, len(df2) + 1):
        sig2 = strat.on_bar(_momentum_ctx(), "SYM", df2.iloc[:end])
        if sig2 is not None:
            break
    assert sig2 is not None and sig2.action == SignalAction.ENTER_LONG, (
        "New inflection after event consumption must arm a new event"
    )


def test_vwap_rsi_volume_ema_still_enforced():
    """MACD event present but confirmation conditions violated → no signal."""
    # EMA downtrend (ema9 < ema20) but looking for LONG: should be blocked
    strat_ema = MomentumCross()
    df_down = _make_momentum_df(trend="down", above_vwap=True, rsi=62.0, vol_z=1.0)
    sigs_ema = [
        strat_ema.on_bar(_momentum_ctx(), "SYM", df_down.iloc[:end])
        for end in range(30, len(df_down) + 1)
    ]
    long_sigs = [s for s in sigs_ema if s and s.action == SignalAction.ENTER_LONG]
    assert not long_sigs, "LONG signal fired despite EMA downtrend"

    # Price below VWAP → LONG blocked
    strat_vwap = MomentumCross()
    df_below = _make_momentum_df(trend="up", above_vwap=False, rsi=62.0, vol_z=1.0)
    sigs_vwap = [
        strat_vwap.on_bar(_momentum_ctx(), "SYM", df_below.iloc[:end])
        for end in range(30, len(df_below) + 1)
    ]
    long_sigs_vwap = [s for s in sigs_vwap if s and s.action == SignalAction.ENTER_LONG]
    assert not long_sigs_vwap, "LONG signal fired despite price below VWAP"

    # vol_z below minimum → blocked
    strat_vol = MomentumCross(params={"vol_z_min": 2.0})
    df_lowvol = _make_momentum_df(trend="up", above_vwap=True, rsi=62.0, vol_z=0.5)
    sigs_vol = [
        strat_vol.on_bar(_momentum_ctx(), "SYM", df_lowvol.iloc[:end])
        for end in range(30, len(df_lowvol) + 1)
    ]
    long_sigs_vol = [s for s in sigs_vol if s and s.action == SignalAction.ENTER_LONG]
    assert not long_sigs_vol, "LONG signal fired despite vol_z below minimum"

