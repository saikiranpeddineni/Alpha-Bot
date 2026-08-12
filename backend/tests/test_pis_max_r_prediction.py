"""Tests for PIS Dynamic Max R Prediction and Runner Expansion Engine."""

import pytest
from datetime import datetime, timezone, timedelta
from app.pis.models import PositionObservation, PISConfig, ExitAction, ReasonCode, TradeHealth
from app.pis.position_brain import PositionBrain
from app.pis.runner_intelligence import RunnerIntelligence
from app.pis.protection import AdaptiveProtectionPlanner
from app.pis.exit_planner import ExitPlanner


def _make_obs(
    symbol: str = "NVDA",
    price: float = 100.0,
    entry: float = 100.0,
    stop: float = 98.0,
    take_profit: float = 104.0,
    side: float = 1.0,
    atr: float = 1.0,
    qty: float = 10.0,
    r_multiple: float = 0.0,
    high: float = 100.0,
    low: float = 100.0,
    volume: float = 50000.0,
    minute_offset: int = 0,
) -> PositionObservation:
    risk = abs(entry - stop)
    return PositionObservation(
        timestamp=datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=minute_offset),
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        current_price=price,
        high=high,
        low=low,
        close=price,
        volume=volume,
        atr=atr,
        initial_stop=stop,
        risk_per_share=risk,
        current_stop=stop,
        take_profit=take_profit,
        r_multiple=r_multiple,
    )


class TestMaxRPredictionEngine:
    """Validate quantitative Max R prediction and runner expansion."""

    def test_dynamic_max_r_expansion_on_high_momentum(self):
        """High momentum + strong RVOL expands predicted ceiling beyond 2.0R."""
        config = PISConfig()
        runner_intel = RunnerIntelligence(config)
        
        obs = _make_obs(entry=100.0, price=102.0, stop=98.0, take_profit=104.0)  # Baseline 2.0R
        strong_health = TradeHealth(
            overall=0.85,
            trend_score=0.90,
            momentum_score=0.85,
            volume_score=0.80,
            volatility_score=0.80,
        )
        
        pred_max_r, runner_prob = runner_intel.predict_max_r(obs, peak_r=1.0, health=strong_health)
        
        assert pred_max_r >= 4.0, f"Expected dynamic Max R >= 4.0R, got {pred_max_r}"
        assert runner_prob >= 0.70, f"Expected runner probability >= 0.70, got {runner_prob}"

    def test_conservative_target_on_weak_setup(self):
        """Low health and volume keeps predicted ceiling at baseline 2.0R."""
        config = PISConfig()
        runner_intel = RunnerIntelligence(config)
        
        obs = _make_obs(entry=100.0, price=100.5, stop=98.0, take_profit=104.0)
        weak_health = TradeHealth(
            overall=0.45,
            trend_score=0.40,
            momentum_score=0.30,
            volume_score=0.25,
            volatility_score=0.50,
        )
        
        pred_max_r, runner_prob = runner_intel.predict_max_r(obs, peak_r=0.25, health=weak_health)
        
        assert pred_max_r == 2.0, f"Expected conservative 2.0R target, got {pred_max_r}"
        assert runner_prob < 0.40, f"Expected low runner probability, got {runner_prob}"

    def test_proximity_trailing_compression(self):
        """Protection planner compresses giveback as price nears predicted ceiling."""
        config = PISConfig()
        protection_planner = AdaptiveProtectionPlanner(config)
        
        obs = _make_obs(entry=100.0, price=107.0, stop=98.0, take_profit=108.0, atr=1.0)
        strong_health = TradeHealth(overall=0.80, trend_score=0.80, momentum_score=0.80, volume_score=0.80, volatility_score=0.80)
        
        # When far below ceiling (e.g. Peak R = 1.5 with Max R = 5.0) -> Wide trail
        floor_wide = protection_planner.compute_protection_floor(
            obs=obs, peak_r=1.5, health=strong_health, has_scaled_out=True, predicted_max_r=5.0
        )
        
        # When nearing ceiling (e.g. Peak R = 4.8 with Max R = 5.0, proximity = 96%) -> Tight trail
        floor_tight = protection_planner.compute_protection_floor(
            obs=obs, peak_r=4.8, health=strong_health, has_scaled_out=True, predicted_max_r=5.0
        )
        
        # Tight floor must lock substantially more profit than wide floor
        assert floor_tight > floor_wide, f"Expected tighter floor near ceiling: tight={floor_tight}, wide={floor_wide}"

    def test_max_r_ceiling_harvesting_exit(self):
        """Reaching predicted max R ceiling with momentum exhaustion triggers full harvest."""
        config = PISConfig()
        exit_planner = ExitPlanner(config)
        
        obs = _make_obs(entry=100.0, price=108.0, stop=98.0, take_profit=104.0, high=108.0, low=107.5)
        exhausted_health = TradeHealth(overall=0.55, trend_score=0.60, momentum_score=0.30, volume_score=0.40, volatility_score=0.50)
        
        from app.pis.models import LifecycleState, ThesisDrift
        thesis = ThesisDrift(drift_score=0.1, adverse_excursion_r=0.0, stagnation_bars=0, is_degraded=False)
        
        decision = exit_planner.synthesize_decision(
            obs=obs,
            lifecycle=LifecycleState.MATURING,
            health=exhausted_health,
            thesis=thesis,
            runner_score=0.90,
            is_runner=True,
            target_stop=106.0,
            peak_r=4.0,
            trough_r=0.0,
            history_len=20,
            has_scaled_out=True,
            predicted_max_r=4.0,
            runner_probability=0.85,
        )
        
        assert decision.action == ExitAction.FULL_EXIT
        assert ReasonCode.MAX_R_CEILING_REACHED in decision.reason_codes

    def test_full_brain_lifecycle_with_max_r_prediction(self):
        """Verify end-to-end PositionBrain lifecycle with dynamic Max R prediction."""
        config = PISConfig(scale_out_target_r=1.0)
        obs0 = _make_obs(price=100.0, entry=100.0, stop=98.0, take_profit=104.0, minute_offset=0)
        brain = PositionBrain("NVDA", obs0, config=config)
        
        # Initial state validation
        assert brain.last_decision.predicted_max_r >= 2.0
        
        # Bar 1: Strong Momentum to +1.0R -> 50% Scale-Out
        obs1 = _make_obs(price=102.0, entry=100.0, stop=98.0, take_profit=104.0, high=102.2, low=100.0, r_multiple=1.0, minute_offset=1)
        d1 = brain.observe(obs1)
        assert d1.action == ExitAction.PARTIAL_EXIT
        # In production, the engine sets this after broker confirmation, simulate by passing reduced qty
        obs1_fill = _make_obs(price=102.0, entry=100.0, stop=98.0, take_profit=104.0, high=102.2, low=100.0, r_multiple=1.0, minute_offset=1, qty=5.0)
        brain.observe(obs1_fill)
        assert brain.has_scaled_out is True
        
        # Bar 2: Runner breathes and runs to +3.5R
        obs2 = _make_obs(price=107.0, entry=100.0, stop=98.0, take_profit=104.0, high=107.2, low=106.5, r_multiple=3.5, minute_offset=2, qty=5.0)
        d2 = brain.observe(obs2)
        assert d2.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
        assert d2.target_stop_loss > 100.0  # Stop locked well above breakeven
