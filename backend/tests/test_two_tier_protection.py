"""Unit tests for Two-Tier Protection Architecture (Tier 1 Soft Technical & Tier 2 Catastrophe Breaker)."""

from datetime import datetime, timezone
import pytest

from app.pis.exit_planner import ExitPlanner
from app.pis.models import (
    ExitAction,
    LifecycleState,
    PISConfig,
    PositionObservation,
    ReasonCode,
    ThesisDrift,
    TradeHealth,
)


def _make_obs(
    symbol: str = "AAPL",
    side: float = 1.0,
    qty: float = 100.0,
    entry_price: float = 100.0,
    current_price: float = 100.0,
    low: float = 100.0,
    high: float = 100.0,
    initial_stop: float = 95.0,
    current_stop: float = 95.0,
    atr: float = 2.0,
) -> PositionObservation:
    risk_per_share = abs(entry_price - initial_stop) if initial_stop else atr * 2.0
    r_multiple = ((current_price - entry_price) / risk_per_share) * side
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        current_price=current_price,
        high=high,
        low=low,
        close=current_price,
        volume=10000.0,
        atr=atr,
        initial_stop=initial_stop,
        risk_per_share=risk_per_share,
        current_stop=current_stop,
        r_multiple=r_multiple,
    )


def test_tier2_hard_catastrophe_breaker() -> None:
    """Position down -$550 must be immediately liquidated by Tier 2 regardless of thesis."""
    config = PISConfig(enable_two_tier_protection=True, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # 100 shares @ $100, drops to $94.50 -> loss is -$550
    # initial_stop is way down at $80.00 (structural stop is not breached)
    obs = _make_obs(qty=100.0, entry_price=100.0, current_price=94.50, low=94.50, high=95.00, initial_stop=80.00)
    health = TradeHealth(momentum_score=0.70, trend_score=0.70, volume_score=0.70, volatility_score=0.70, overall=0.70)
    thesis = ThesisDrift(drift_score=0.10, adverse_excursion_r=0.10, stagnation_bars=1, is_degraded=False)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.VALIDATED,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=80.00,
        peak_r=0.0,
        trough_r=-0.25,
        history_len=10,
    )

    assert decision.action == ExitAction.FULL_EXIT
    assert decision.exit_ratio == 1.0
    assert ReasonCode.TIER2_CATASTROPHE_BREAKER in decision.reason_codes
    assert ReasonCode.EMERGENCY_STOP in decision.reason_codes
    assert decision.metadata["tier2_catastrophe_breached"] is True


def test_tier1_soft_breach_when_structure_broken() -> None:
    """Position down -$350 with structural stop broken must exit cleanly at Tier 1."""
    config = PISConfig(enable_two_tier_protection=True, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # 100 shares @ $100, drops to $96.50 -> loss is -$350
    # initial_stop is $97.00 -> low of $96.50 breaches initial_stop
    obs = _make_obs(qty=100.0, entry_price=100.0, current_price=96.50, low=96.50, high=97.00, initial_stop=97.00)
    health = TradeHealth(momentum_score=0.30, trend_score=0.30, volume_score=0.30, volatility_score=0.30, overall=0.30)
    thesis = ThesisDrift(drift_score=0.60, adverse_excursion_r=1.16, stagnation_bars=5, is_degraded=True)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.WEAKENING,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=97.00,
        peak_r=0.0,
        trough_r=-1.16,
        history_len=10,
    )

    assert decision.action == ExitAction.FULL_EXIT
    assert decision.exit_ratio == 1.0
    assert ReasonCode.TIER1_SOFT_TECHNICAL in decision.reason_codes
    assert ReasonCode.PROTECTION_BREACH in decision.reason_codes
    assert decision.metadata["tier1_soft_breached"] is True
    assert decision.metadata["tier2_catastrophe_breached"] is False


def test_tier1_breathes_when_structure_intact() -> None:
    """Position down -$350 on spread noise with structural stop intact must breathe (HOLD)."""
    config = PISConfig(enable_two_tier_protection=True, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # 100 shares @ $100, drops to $96.50 -> loss is -$350
    # initial_stop is at $95.00 -> low of $96.50 has NOT breached initial_stop
    obs = _make_obs(qty=100.0, entry_price=100.0, current_price=96.50, low=96.50, high=97.50, initial_stop=95.00)
    health = TradeHealth(momentum_score=0.65, trend_score=0.65, volume_score=0.65, volatility_score=0.65, overall=0.65)
    thesis = ThesisDrift(drift_score=0.20, adverse_excursion_r=0.70, stagnation_bars=1, is_degraded=False)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.VALIDATED,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=95.00,
        peak_r=0.10,
        trough_r=-0.70,
        history_len=5,
    )

    # Must NOT panic exit!
    assert decision.action == ExitAction.HOLD
    assert decision.metadata["tier1_breathed"] is True
    assert decision.metadata["tier1_soft_breached"] is False
    assert decision.metadata["tier2_catastrophe_breached"] is False


def test_two_tier_disabled_preserves_legacy() -> None:
    """When disabled, Two-Tier logic does not intervene."""
    config = PISConfig(enable_two_tier_protection=False, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # Down -$550 with intact stop
    obs = _make_obs(qty=100.0, entry_price=100.0, current_price=94.50, low=94.50, high=95.00, initial_stop=80.00)
    health = TradeHealth(momentum_score=0.70, trend_score=0.70, volume_score=0.70, volatility_score=0.70, overall=0.70)
    thesis = ThesisDrift(drift_score=0.10, adverse_excursion_r=0.10, stagnation_bars=1, is_degraded=False)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.VALIDATED,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=80.00,
        peak_r=0.0,
        trough_r=-0.25,
        history_len=10,
    )

    assert decision.action == ExitAction.HOLD
    assert decision.metadata["tier2_catastrophe_breached"] is False


def test_two_tier_config_validation() -> None:
    """PISConfig must enforce tier1_loss_usd > 0 and tier2_loss_usd > tier1_loss_usd."""
    with pytest.raises(ValueError, match="tier1_loss_usd must be > 0"):
        PISConfig(tier1_loss_usd=0.0).validate()

    with pytest.raises(ValueError, match="tier2_loss_usd must be > tier1_loss_usd"):
        PISConfig(tier1_loss_usd=500.0, tier2_loss_usd=300.0).validate()


def test_short_position_two_tier() -> None:
    """Verify Two-Tier protection behaves symmetrically on short positions."""
    config = PISConfig(enable_two_tier_protection=True, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # SHORT 100 shares @ $100, price rises to $105.50 -> loss is -$550
    obs = _make_obs(side=-1.0, qty=100.0, entry_price=100.0, current_price=105.50, low=104.0, high=105.50, initial_stop=110.00)
    health = TradeHealth(momentum_score=0.70, trend_score=0.70, volume_score=0.70, volatility_score=0.70, overall=0.70)
    thesis = ThesisDrift(drift_score=0.10, adverse_excursion_r=0.10, stagnation_bars=1, is_degraded=False)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.VALIDATED,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=110.00,
        peak_r=0.0,
        trough_r=-0.55,
        history_len=10,
    )
    assert decision.action == ExitAction.FULL_EXIT
    assert ReasonCode.TIER2_CATASTROPHE_BREAKER in decision.reason_codes


def test_live_quote_triggers_tier2_without_bar_close_lag() -> None:
    """Live tick drop in current_price must trigger Tier 2 immediately without waiting for bar close."""
    config = PISConfig(enable_two_tier_protection=True, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # Completed bar low is still $99.00 (-$100), but live quote just flashed down to $94.50 (-$550)
    obs = _make_obs(qty=100.0, entry_price=100.0, current_price=94.50, low=99.00, high=100.00, initial_stop=80.00)
    health = TradeHealth(momentum_score=0.70, trend_score=0.70, volume_score=0.70, volatility_score=0.70, overall=0.70)
    thesis = ThesisDrift(drift_score=0.10, adverse_excursion_r=0.10, stagnation_bars=1, is_degraded=False)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.VALIDATED,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=80.00,
        peak_r=0.0,
        trough_r=-0.25,
        history_len=10,
    )
    assert decision.action == ExitAction.FULL_EXIT
    assert ReasonCode.TIER2_CATASTROPHE_BREAKER in decision.reason_codes


def test_live_quote_triggers_tier1_structural_breach() -> None:
    """Live tick drop below initial_stop with -$350 loss must trigger Tier 1 immediately."""
    config = PISConfig(enable_two_tier_protection=True, tier1_loss_usd=300.0, tier2_loss_usd=500.0)
    planner = ExitPlanner(config)

    # Completed bar low is $98.00, but live quote drops to $96.50 (-$350), breaking initial_stop $97.00
    obs = _make_obs(qty=100.0, entry_price=100.0, current_price=96.50, low=98.00, high=100.00, initial_stop=97.00)
    health = TradeHealth(momentum_score=0.65, trend_score=0.65, volume_score=0.65, volatility_score=0.65, overall=0.65)
    thesis = ThesisDrift(drift_score=0.20, adverse_excursion_r=0.50, stagnation_bars=1, is_degraded=False)

    decision = planner.synthesize_decision(
        obs=obs,
        lifecycle=LifecycleState.VALIDATED,
        health=health,
        thesis=thesis,
        runner_score=0.0,
        is_runner=False,
        target_stop=97.00,
        peak_r=0.0,
        trough_r=-0.50,
        history_len=10,
    )
    assert decision.action == ExitAction.FULL_EXIT
    assert ReasonCode.TIER1_SOFT_TECHNICAL in decision.reason_codes
