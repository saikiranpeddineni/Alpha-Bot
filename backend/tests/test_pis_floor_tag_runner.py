"""Floor-tag runner + anti-panic exit scenario tests.

Evidence base (29 sessions, 565 R-measured trades):
  - 38% of exits were followed by >= +1R continuation (~696 R left behind)
  - Winners average only ~0.44R peak -> the 2R scale-out gate rarely arms
  - Thesis-intact floor tags continued +2.18R on average afterwards
Scenarios pinned here so the behavior cannot silently regress.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.pis.exit_planner import ExitPlanner
from app.pis.models import (
    ExitAction,
    LifecycleState,
    PISConfig,
    PositionObservation,
    ThesisDrift,
    TradeHealth,
)


def _obs(r: float = -0.98, side: float = 1.0, qty: float = 100.0) -> PositionObservation:
    entry, risk = 100.0, 1.0
    px = entry + r * risk * side
    return PositionObservation(
        timestamp=datetime.now(UTC),
        symbol="AAPL",
        side=side,
        qty=qty,
        entry_price=entry,
        current_price=px,
        high=max(px, entry),
        low=min(px, entry),
        close=px,
        volume=1000.0,
        atr=0.5,
        initial_stop=entry - side * risk,
        risk_per_share=risk,
        current_stop=None,
        take_profit=None,
        r_multiple=r,
        news_sentiment_score=0.0,
    )


def _health(score: float = 0.70) -> TradeHealth:
    return TradeHealth(overall=score, trend_score=score, momentum_score=score,
                       volatility_score=0.8, volume_score=0.8, news_sentiment=0.0)


def _thesis(degraded: bool = False, early: bool = False) -> ThesisDrift:
    return ThesisDrift(drift_score=0.9 if degraded else 0.1,
                       adverse_excursion_r=0.95 if degraded else 0.3,
                       stagnation_bars=0, is_degraded=degraded,
                       early_invalidation_detected=early)


def _plan(obs, thesis, health=None, lifecycle=LifecycleState.VALIDATED,
          target_stop=None, has_scaled_out=False, cfg=None):
    obs = SimpleNamespace(**{**obs.__dict__, "r_multiple": obs.r_multiple})
    target_stop = target_stop if target_stop is not None else (obs.current_price + 10.0 * obs.side * -1)
    # protection breach: price beyond floor
    return ExitPlanner(cfg or PISConfig()).synthesize_decision(
        obs=obs, lifecycle=lifecycle, health=health or _health(),
        thesis=thesis, runner_score=0.2, is_runner=False,
        target_stop=target_stop, peak_r=0.4, trough_r=-1.0,
        history_len=12, has_scaled_out=has_scaled_out,
        predicted_max_r=3.0, runner_probability=0.1,
    )


def test_floor_tag_with_intact_thesis_banks_half_not_all():
    """S1: floor tagged, thesis intact -> PARTIAL (runner keeps same-floor stop)."""
    obs = _obs(r=-0.98)  # price tagged the initial stop
    d = _plan(obs, _thesis(degraded=False), target_stop=99.02)
    assert d.action == ExitAction.PARTIAL_EXIT
    assert d.exit_ratio == pytest.approx(PISConfig().floor_tag_exit_ratio)
    assert any(c.value == "FLOOR_TAG_RUNNER" for c in d.reason_codes)


def test_floor_tag_with_degraded_thesis_still_full_exits():
    """S2: degradation is structural failure -- no runner on a dead trade."""
    obs = _obs(r=-0.95)
    d = _plan(obs, _thesis(degraded=True), target_stop=99.05)
    assert d.action == ExitAction.FULL_EXIT
    assert d.exit_ratio == 1.0


def test_early_invalidation_scratch_unchanged():
    """S3: the asymmetric early cut stays FULL and first-priority."""
    obs = _obs(r=-0.70)
    d = _plan(obs, _thesis(early=True), lifecycle=LifecycleState.NEW, target_stop=99.3)
    assert d.action == ExitAction.FULL_EXIT
    assert any(c.value == "EARLY_THESIS_INVALIDATED" for c in d.reason_codes)


def test_second_floor_tag_after_scaleout_fully_exits():
    """S5: remainder never loops partials -- second tag is the final exit."""
    obs = _obs(r=-0.98)
    d = _plan(obs, _thesis(False), target_stop=99.02, has_scaled_out=True)
    assert d.action == ExitAction.FULL_EXIT


def test_floor_tag_runner_disabled_restores_full_cut():
    """Kill-switch: config off -> legacy full-exit behavior."""
    cfg = PISConfig(enable_floor_tag_runner=False)
    obs = _obs(r=-0.98)
    d = _plan(obs, _thesis(False), target_stop=99.02, cfg=cfg)
    assert d.action == ExitAction.FULL_EXIT


def test_distribution_trim_requires_profit():
    """S6: distribution label must not panic-sell losers; profits may trim."""
    cfg = PISConfig()
    planner = ExitPlanner(cfg)

    class _P:
        def synthesize(self, r):
            obs = _obs(r=r)
            return planner.synthesize_decision(
                obs=obs, lifecycle=LifecycleState.DISTRIBUTION,
                health=_health(0.20), thesis=_thesis(False),
                runner_score=0.1, is_runner=False,
                target_stop=80.0,  # far away -> no breach
                peak_r=max(0.4, r), trough_r=min(-0.2, r),
                history_len=30, has_scaled_out=False,
                predicted_max_r=3.0, runner_probability=0.1,
            )

    loser = _P().synthesize(r=-0.40)
    assert loser.action != ExitAction.PARTIAL_EXIT, "no distribution trim at a loss"
    winner = _P().synthesize(r=+0.60)
    assert winner.action == ExitAction.PARTIAL_EXIT


def test_healthy_hold_is_untouched():
    """No floor tag, healthy position -> never exits (HOLD or floor-ratchet update)."""
    obs = _obs(r=+0.50)
    d = _plan(obs, _thesis(False), _health(0.85), target_stop=95.0)
    assert d.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
