from __future__ import annotations

from app.config import Settings
from app.core.domain import Position, Signal, SignalAction
from app.portfolio.allocator import (
    CapitalAllocationOptimizer,
    PortfolioAIGate,
    process_portfolio_allocations,
)


def _signal(symbol: str, score: float, ml_probability: float) -> Signal:
    sig = Signal(
        symbol=symbol,
        action=SignalAction.ENTER_LONG,
        strategy="momentum",
        price=100.0,
        stop_loss=99.0,
        take_profit=100.1,  # deliberately negative Kelly under old invalid event pairing
        reason="test",
    )
    sig.score = score
    sig.ml_probability = ml_probability
    return sig


def test_settings_risk_config_propagates_all_scanner_and_model_controls(monkeypatch):
    monkeypatch.setenv("MIN_PRICE", "17.5")
    monkeypatch.setenv("MIN_AVG_DOLLAR_VOLUME", "2345678")
    monkeypatch.setenv("MIN_SPECIALIST_AUC", "0.61")
    monkeypatch.setenv("MIN_STOP_PCT", "0.83")
    settings = Settings(_env_file=None)
    risk = settings.risk_config()
    assert risk.min_price == 17.5
    assert risk.min_avg_dollar_volume == 2345678
    assert risk.min_specialist_auc == 0.61
    assert risk.min_stop_pct == 0.83


def test_allocator_never_uses_incompatible_kelly_as_trade_gate():
    sig = _signal("AAPL", score=0.80, ml_probability=0.55)
    decision = process_portfolio_allocations(
        [sig], [], available_capital=10_000, equity=100_000,
        portfolio_allocator=CapitalAllocationOptimizer(),
    )
    assert decision.approved_entries == [(sig, None)]
    assert decision.gated_entries == []


def test_allocator_negative_kelly_branch_cannot_crash_or_reject():
    signals = [_signal("AAPL", 0.8, 0.51), _signal("MSFT", 0.7, 0.51)]
    decision = process_portfolio_allocations(
        signals, [], available_capital=20_000, equity=100_000,
        portfolio_allocator=CapitalAllocationOptimizer(),
    )
    assert [sig.symbol for sig, _ in decision.approved_entries] == ["AAPL", "MSFT"]


def test_unknown_symbols_do_not_share_fake_other_sector():
    gate = PortfolioAIGate(max_positions_per_sector=1)
    positions = [Position(symbol="UNKNOWN_A", qty=1, avg_entry_price=10, market_price=10, strategy="test")]
    approved, reason = gate.gate("UNKNOWN_B", positions)
    assert approved, reason
