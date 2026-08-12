"""Champion-vs-candidate promotion invariants.

These tests protect against optimizing for raw P&L at the expense of expectancy,
profit capture, or materially worse drawdown. A future data-backed evaluator can
reuse the same contract with real replay summaries.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Snapshot:
    net_r: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r: float
    profit_capture: float


def non_regressive(champion: Snapshot, candidate: Snapshot, max_drawdown_degradation_r: float = 1.0) -> bool:
    return (
        candidate.net_r >= champion.net_r
        and candidate.expectancy_r >= champion.expectancy_r
        and candidate.profit_factor >= champion.profit_factor
        and candidate.max_drawdown_r <= champion.max_drawdown_r + max_drawdown_degradation_r
        and candidate.profit_capture >= champion.profit_capture
    )


def test_candidate_with_better_profit_and_quality_can_replace_champion():
    champion = Snapshot(100, .20, 1.45, 12, .38)
    candidate = Snapshot(115, .24, 1.58, 11, .44)
    assert non_regressive(champion, candidate)


def test_higher_profit_with_bad_drawdown_is_rejected():
    champion = Snapshot(100, .20, 1.45, 12, .38)
    candidate = Snapshot(130, .22, 1.50, 25, .31)
    assert not non_regressive(champion, candidate)


def test_more_trades_do_not_justify_lower_expectancy():
    champion = Snapshot(100, .20, 1.45, 12, .38)
    candidate = Snapshot(98, .17, 1.38, 13, .30)
    assert not non_regressive(champion, candidate)


def test_lower_profit_capture_is_a_non_regression_failure():
    champion = Snapshot(100, .20, 1.45, 12, .38)
    candidate = Snapshot(110, .23, 1.52, 12.2, .35)
    assert not non_regressive(champion, candidate)
