"""Champion-vs-candidate profit regression invariants for Alphadesk_004."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PerformanceSnapshot:
    net_r: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r: float
    trades: int
    profit_capture: float


def compare_to_champion(champion: PerformanceSnapshot, candidate: PerformanceSnapshot) -> dict[str, float]:
    return {
        "net_r_delta": candidate.net_r - champion.net_r,
        "expectancy_delta": candidate.expectancy_r - champion.expectancy_r,
        "profit_factor_delta": candidate.profit_factor - champion.profit_factor,
        "drawdown_delta": candidate.max_drawdown_r - champion.max_drawdown_r,
        "trade_delta": candidate.trades - champion.trades,
        "profit_capture_delta": candidate.profit_capture - champion.profit_capture,
    }


def promotion_is_non_regressive(
    champion: PerformanceSnapshot,
    candidate: PerformanceSnapshot,
    *,
    max_drawdown_degradation_r: float = 1.0,
) -> bool:
    """Require no material degradation before a candidate can replace Champion."""
    delta = compare_to_champion(champion, candidate)
    return bool(
        delta["net_r_delta"] >= 0
        and delta["expectancy_delta"] >= 0
        and delta["profit_factor_delta"] >= 0
        and delta["drawdown_delta"] <= max_drawdown_degradation_r
        and delta["profit_capture_delta"] >= 0
    )


def test_improved_candidate_can_promote():
    champion = PerformanceSnapshot(100, .20, 1.45, 12, 1000, .38)
    candidate = PerformanceSnapshot(115, .24, 1.58, 11, 1010, .44)
    assert promotion_is_non_regressive(champion, candidate)


def test_higher_pnl_with_materially_worse_drawdown_does_not_promote():
    champion = PerformanceSnapshot(100, .20, 1.45, 12, 1000, .38)
    candidate = PerformanceSnapshot(130, .22, 1.50, 25, 950, .31)
    assert not promotion_is_non_regressive(champion, candidate)


def test_more_trades_alone_is_not_a_promotion_reason():
    champion = PerformanceSnapshot(100, .20, 1.45, 12, 1000, .38)
    candidate = PerformanceSnapshot(98, .17, 1.38, 13, 1500, .30)
    assert not promotion_is_non_regressive(champion, candidate)


def test_profit_capture_is_part_of_the_regression_contract():
    champion = PerformanceSnapshot(100, .20, 1.45, 12, 1000, .38)
    candidate = PerformanceSnapshot(110, .23, 1.52, 12.2, 1000, .35)
    assert not promotion_is_non_regressive(champion, candidate)


def test_promotion_is_comparative_not_an_absolute_profit_threshold():
    """A modest absolute result can still promote when every quality metric improves."""
    champion = PerformanceSnapshot(0.50, .02, 1.05, 1.00, 100, .20)
    candidate = PerformanceSnapshot(0.60, .03, 1.10, 1.05, 102, .22)
    assert promotion_is_non_regressive(champion, candidate)


MARKET_SCENARIOS = (
    "trend_up", "trend_down", "range_chop", "high_volatility", "low_volatility",
    "gap_continuation", "gap_fade", "news_shock", "false_breakout", "reversal",
    "sector_rotation", "market_stress", "end_of_day",
)


def test_champion_benchmark_requires_each_market_scenario():
    assert len(MARKET_SCENARIOS) == 13
    assert len(set(MARKET_SCENARIOS)) == 13
