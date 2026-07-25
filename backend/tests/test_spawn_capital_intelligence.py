from datetime import datetime, timedelta, timezone

from app.research.spawn.spawn_capital_intelligence import analyze_closed_trades, velocity_score


def trade(minutes=10, pnl=10.0, r=1.0, notional=100.0, start_minute=0):
    start = datetime(2026, 7, 24, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=start_minute)
    return {
        "entry_at": start.isoformat(),
        "exit_at": (start + timedelta(minutes=minutes)).isoformat(),
        "pnl": pnl,
        "realized_r": r,
        "entry_notional": notional,
    }


def test_base_requires_realized_evidence():
    info = analyze_closed_trades([trade()] * 2, min_evidence_trades=5)
    assert info.state == "BASE"
    assert info.profit_reinvestment_multiplier == 1.0


def test_compound_requires_completed_wins_and_realized_r():
    trades = [trade(pnl=5, r=1)] * 5
    info = analyze_closed_trades(trades, min_evidence_trades=5, compound_r=4.0)
    assert info.state == "COMPOUND"
    assert 1.0 < info.profit_reinvestment_multiplier <= 1.5


def test_loss_after_compound_evidence_enters_protect():
    trades = [trade(pnl=5, r=1)] * 5 + [trade(pnl=-5, r=-1)]
    info = analyze_closed_trades(trades, min_evidence_trades=5, compound_r=4.0)
    assert info.state == "PROTECT"
    assert info.profit_reinvestment_multiplier == 1.0


def test_capital_minutes_are_duration_weighted():
    info = analyze_closed_trades([trade(minutes=10, pnl=10, notional=100)], min_evidence_trades=1)
    assert info.capital_minutes == 1000.0
    assert info.pnl_per_capital_minute == 0.01


def test_velocity_does_not_reward_losing_churn():
    info = analyze_closed_trades([trade(minutes=1, pnl=-1, r=-1, notional=100)] * 10, min_evidence_trades=5)
    assert velocity_score(info) == 0.0


def test_invalid_timestamps_cannot_create_fake_velocity():
    bad = {"entry_at": "bad", "exit_at": "bad", "pnl": 10, "realized_r": 1, "entry_notional": 100}
    info = analyze_closed_trades([bad] * 5, min_evidence_trades=5)
    assert info.capital_minutes == 0.0
    assert info.pnl_per_capital_minute == 50.0  # denominator is safely floored; evidence remains visible
