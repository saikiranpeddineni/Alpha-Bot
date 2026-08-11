from app.core.domain import SafetyDecision
from app.core.scanner import MarketScanner
from app.strategies.pipeline import SignalPipeline


def test_safety_decision_exposes_canonical_market_state_to_portfolio_gate():
    decision = SafetyDecision(
        system_state="NORMAL",
        market_risk_state="CAUTION",
        allow_new_entries=True,
        risk_multiplier=1.0,
        allowed_strategies=["momentum"],
        require_exit_only=False,
    )
    assert decision.market_state == decision.market_risk_state == "CAUTION"


def test_champion_pipeline_boundary_is_installed():
    assert getattr(SignalPipeline.process, "_champion_boundary_installed", False) is True


def test_scanner_mover_context_bridge_is_installed():
    assert getattr(MarketScanner._stage1_symbols, "_mover_bridge_installed", False) is True
