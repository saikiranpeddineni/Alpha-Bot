"""Deterministic market-condition fixtures used by the main regression suite.

These are deliberately synthetic behavioral fixtures, not claims about future
market prices. Real historical replay remains a separate validation layer.
Each fixture describes a plausible intraday structure and the evidence supplied
to the existing entry-intelligence boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.domain import Regime


@dataclass(frozen=True)
class MarketScenario:
    name: str
    side: str
    closes: tuple[float, ...]
    regime: Regime
    mover_bias: str | None
    rs_score: float | None
    expect_allowed: bool
    description: str


SCENARIOS = (
    MarketScenario("trend_up", "long", tuple(100 + i * .35 for i in range(30)), Regime.TREND_UP, "gainer", .70, True, "persistent directional continuation"),
    MarketScenario("trend_down", "short", tuple(110 - i * .35 for i in range(30)), Regime.TREND_DOWN, "loser", .30, True, "persistent downside continuation"),
    MarketScenario("range_chop", "long", tuple(100 + (.12 if i % 2 == 0 else -.12) for i in range(30)), Regime.RANGE, None, None, True, "range without enough evidence for a blanket veto"),
    MarketScenario("high_volatility", "long", (100,100.8,99.6,101.0,99.8,101.4,100.2,102.0,101.1,102.8,103.4,104.1), Regime.VOLATILE, "gainer", .68, True, "large alternating bars with eventual directional confirmation"),
    MarketScenario("low_volatility", "long", tuple(100 + i * .03 for i in range(30)), Regime.RANGE, "gainer", .64, True, "compressed move with symbol confirmation"),
    MarketScenario("gap_continuation", "long", (100,100.1,103,103.4,103.8,104.1,104.5,104.9,105.2,105.7,106.1,106.4), Regime.TREND_UP, "gainer", .72, True, "gap followed by continuation"),
    MarketScenario("gap_fade", "long", (100,100.1,104,103.6,103.0,102.3,101.5,100.8,100.2,99.8,99.4,99.0), Regime.RANGE, "neutral", .50, False, "gap reverses with weak confirmation"),
    MarketScenario("news_shock_absorbed", "long", tuple(100 + i * .22 for i in range(30)), Regime.TREND_UP, "gainer", .70, True, "adverse headline is absorbed by price"),
    MarketScenario("false_breakout", "long", (100,100.2,100.4,100.6,100.8,101.1,101.5,101.0,100.5,100.0,99.6,99.2), Regime.RANGE, "neutral", .50, False, "breakout attempt fails"),
    MarketScenario("reversal_long", "long", (105,104.7,104.4,104.1,103.8,103.5,103.7,104.0,104.4,104.9,105.5,106.0), Regime.TREND_DOWN, None, None, True, "counter-trend signal receives structural confirmation"),
    MarketScenario("sector_rotation", "long", tuple(100 + i * .18 for i in range(30)), Regime.RANGE, "gainer", .68, True, "symbol strength despite broad range"),
    MarketScenario("market_stress_selective", "long", tuple(100 + i * .12 for i in range(30)), Regime.VOLATILE, "gainer", .72, True, "strong individual confirmation during volatile regime"),
    MarketScenario("end_of_day_continuation", "long", tuple(100 + i * .16 for i in range(30)), Regime.TREND_UP, "gainer", .69, True, "confirmed continuation near session end"),
)
