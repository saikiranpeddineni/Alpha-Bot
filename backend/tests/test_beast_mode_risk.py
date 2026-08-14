from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.domain import Signal, SignalAction
from app.risk.manager import RiskManager, ensure_thesis_fingerprint


class _Clock:
    def in_entry_window(self, _minutes, _now):
        return True

    def session_date(self, now=None):
        return (now or datetime.now(UTC)).date()


def _risk() -> RiskManager:
    from app.config import RiskConfig

    return RiskManager(RiskConfig(), _Clock())


def test_beast_global_wait_after_completed_trade():
    risk = _risk()
    now = datetime.now(UTC)
    trade = SimpleNamespace(
        symbol="ORCL",
        strategy="momentum",
        pnl=-50.0,
        thesis_fingerprint="abc",
    )

    risk.on_trade_closed(trade, now=now)

    signal = Signal(
        symbol="NVDA",
        action=SignalAction.ENTER_LONG,
        strategy="momentum",
        price=180.0,
        strategy_params={"entry_quality_score": 95.0},
    )

    decision = risk.is_entry_permitted(signal, now=now + timedelta(minutes=1))
    assert not decision.approved
    assert "BEAST_WAIT" in decision.reason


def test_beast_historical_three_losses_blocks_symbol():
    risk = _risk()
    now = datetime.now(UTC)
    trades = [
        SimpleNamespace(symbol="AMZN", strategy="momentum", pnl=-10.0, exit_at=now - timedelta(hours=1)),
        SimpleNamespace(symbol="AMZN", strategy="momentum", pnl=-20.0, exit_at=now - timedelta(hours=2)),
        SimpleNamespace(symbol="AMZN", strategy="momentum", pnl=-30.0, exit_at=now - timedelta(hours=3)),
    ]

    risk.reconstruct_cooldowns(trades, now=now)

    signal = Signal(
        symbol="AMZN",
        action=SignalAction.ENTER_SHORT,
        strategy="momentum",
        price=200.0,
        strategy_params={"entry_quality_score": 95.0},
    )

    decision = risk.is_entry_permitted(signal, now=now)
    assert not decision.approved
    assert "behavioral loss loop" in decision.reason


def test_thesis_fingerprint_is_stable_without_exact_price():
    first = Signal(
        symbol="CBRS",
        action=SignalAction.ENTER_SHORT,
        strategy="momentum",
        price=223.40,
        strategy_params={
            "entry_quality_score": 88.0,
            "opportunity_state": "EXTENDED",
            "atr_extension": 1.10,
            "vwap_distance_pct": 1.60,
        },
    )
    second = Signal(
        symbol="CBRS",
        action=SignalAction.ENTER_SHORT,
        strategy="momentum",
        price=224.05,
        strategy_params={
            "entry_quality_score": 87.8,
            "opportunity_state": "EXTENDED",
            "atr_extension": 1.12,
            "vwap_distance_pct": 1.55,
        },
    )

    assert ensure_thesis_fingerprint(first) == ensure_thesis_fingerprint(second)


def test_beast_rejects_extreme_mover_chase_without_catalyst():
    risk = _risk()
    now = datetime.now(UTC)
    signal = Signal(
        symbol="CBRS",
        action=SignalAction.ENTER_SHORT,
        strategy="momentum",
        price=223.40,
        mover_bias="loser",
        mover_change_pct=-11.7,
        news_direction="neutral",
        news_confidence=0.0,
        strategy_params={
            "entry_quality_score": 88.0,
            "atr_extension": 1.10,
            "vwap_distance_pct": 1.70,
        },
    )

    decision = risk.is_entry_permitted(signal, now=now)
    assert not decision.approved
    assert "extreme mover chase" in decision.reason
