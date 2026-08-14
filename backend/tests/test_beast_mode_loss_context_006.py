from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.risk.manager import RiskManager


class DummyClock:
    def in_entry_window(self, *_args, **_kwargs):
        return True


def make_manager():
    config = SimpleNamespace(
        beast_global_cooldown_minutes=10,
        beast_same_thesis_cooldown_minutes=120,
        beast_behavioral_window_days=7,
        beast_behavioral_loss_limit=3,
    )
    return RiskManager(config, DummyClock())


def trade(symbol, pnl, exit_at, thesis="thesis-a"):
    return SimpleNamespace(
        symbol=symbol,
        pnl=pnl,
        exit_at=exit_at,
        thesis_fingerprint=thesis,
        strategy="momentum",
    )


def test_three_recent_losses_do_not_blacklist_symbol():
    manager = make_manager()
    now = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    trades = [
        trade("NVDA", -10, now - timedelta(minutes=30), "t1"),
        trade("NVDA", -8, now - timedelta(hours=2), "t2"),
        trade("NVDA", -6, now - timedelta(hours=4), "t3"),
    ]

    manager.reconstruct_cooldowns(trades, now)

    assert "NVDA" not in manager.symbol_cooldowns
    assert "NVDA" not in manager.behavioral_loop_cooldowns
    assert manager.historical_loss_context["NVDA"]["recent_loss_count"] == 3
    assert manager.historical_loss_context["NVDA"]["behavioral_warning"] is True


def test_strategy_loss_streak_is_context_not_hard_veto():
    manager = make_manager()
    manager.strategy_consecutive_losses["momentum"] = 5

    assert manager.is_strategy_in_cooldown("momentum") is False


def test_losing_thesis_is_blocked_without_blocking_symbol():
    manager = make_manager()
    now = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    manager.on_trade_closed(trade("NVDA", -10, now, "same-thesis"), now)

    assert "NVDA" not in manager.symbol_cooldowns
    assert "NVDA" not in manager.behavioral_loop_cooldowns
    assert manager.closed_theses["same-thesis"] == now
    assert manager.global_cooldown_until == now + timedelta(minutes=10)


def test_old_checkpoint_cannot_restore_retired_symbol_blacklist():
    manager = make_manager()
    manager.restore_risk_state(
        {
            "symbol_cooldowns": {"NVDA": "2026-08-14T16:00:00+00:00"},
            "behavioral_loop_cooldowns": {"NVDA": "2026-08-14T16:00:00+00:00"},
        }
    )

    assert manager.symbol_cooldowns == {}
    assert manager.behavioral_loop_cooldowns == {}
