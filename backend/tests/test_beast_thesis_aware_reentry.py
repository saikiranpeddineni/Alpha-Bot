from datetime import UTC, datetime, timedelta

from app.config import RiskConfig
from app.core.clock import MarketClock
from app.core.domain import Signal, SignalAction
from app.risk.manager import RiskManager, ensure_thesis_fingerprint


NOW = datetime(2026, 8, 14, 14, 0, tzinfo=UTC)


def _signal(symbol: str = "AMZN", strategy: str = "momentum") -> Signal:
    return Signal(
        symbol=symbol,
        action=SignalAction.ENTER_SHORT,
        confidence=0.80,
        strategy=strategy,
        reason="fresh setup",
        price=100.0,
    )


def _risk() -> RiskManager:
    return RiskManager(RiskConfig(), MarketClock(always_open=True))


def test_fresh_thesis_can_reenter_after_symbol_loss_memory():
    risk = _risk()
    risk.symbol_cooldowns["AMZN"] = NOW + timedelta(minutes=60)
    risk.behavioral_loop_cooldowns["AMZN"] = NOW + timedelta(minutes=60)

    decision = risk.is_entry_permitted(_signal(), now=NOW)

    assert decision.approved is True


def test_same_losing_thesis_is_still_blocked():
    risk = _risk()
    signal = _signal()
    fingerprint = ensure_thesis_fingerprint(signal)
    risk.closed_theses[fingerprint] = NOW

    decision = risk.is_entry_permitted(signal, now=NOW + timedelta(minutes=30))

    assert decision.approved is False
    assert "same thesis" in decision.reason.lower()


def test_global_wait_still_prevents_trade_churn():
    risk = _risk()
    risk.global_cooldown_until = NOW + timedelta(minutes=5)
    risk.symbol_cooldowns["AMZN"] = NOW + timedelta(minutes=60)

    decision = risk.is_entry_permitted(_signal(), now=NOW)

    assert decision.approved is False
    assert "global trade cooldown" in decision.reason.lower()
