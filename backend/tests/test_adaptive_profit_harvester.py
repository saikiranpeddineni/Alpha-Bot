import pandas as pd
import pytest

from app.analytics.adaptive_profit_harvester import HarvestConfig, adaptive_stop, replay_adaptive_harvest


def _bars(rows):
    return pd.DataFrame(rows, index=pd.date_range("2026-07-23 10:00", periods=len(rows), freq="min"))


def test_does_not_tighten_before_trade_proves_itself_long():
    d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=101, atr=1)
    assert d.state == "initial_risk"
    assert d.stop_price == 98


def test_breakeven_protection_only_after_trigger():
    d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=101.5, atr=1)
    assert d.state == "breakeven"
    assert d.stop_price == pytest.approx(100.1)


def test_runner_gets_wider_atr_room_than_profit_lock():
    cfg = HarvestConfig()
    lock = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=103.2, atr=1, config=cfg)
    runner = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=105.2, atr=1, config=cfg)
    assert lock.state == "profit_lock"
    assert runner.state == "runner"
    assert cfg.runner_atr_mult > cfg.lock_atr_mult
    assert runner.locked_r >= cfg.runner_floor_r


def test_exceptional_runner_locks_material_profit():
    cfg = HarvestConfig()
    d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=108.5, atr=1, config=cfg)
    assert d.state == "exceptional_runner"
    assert d.locked_r >= cfg.exceptional_floor_r


def test_stop_is_monotonic_for_long():
    first = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=104, atr=1)
    second = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=104.5, atr=1, current_stop=first.stop_price)
    assert second.stop_price >= first.stop_price


def test_stop_is_monotonic_for_short():
    first = adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=96, atr=1)
    second = adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=95.5, atr=1, current_stop=first.stop_price)
    assert second.stop_price <= first.stop_price


def test_never_widens_original_risk_long_or_short():
    assert adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=100.5, atr=3).stop_price >= 98
    assert adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=99.5, atr=3).stop_price <= 102


def test_invalid_zero_risk_is_rejected():
    with pytest.raises(ValueError):
        adaptive_stop(direction="long", entry_price=100, initial_stop=100, prior_peak=101, atr=1)


def test_replay_excludes_ambiguous_entry_bar_extremes():
    df = _bars([
        # These extremes may have occurred before the fill at 100 and must not count.
        {"open":95,"high":110,"low":90,"close":100,"atr14":1.0},
        {"open":100,"high":101,"low":99.5,"close":100.8,"atr14":1.0},
        {"open":100.8,"high":102,"low":100.4,"close":101.8,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r["replay_valid"] is True
    assert r["mfe_r"] == pytest.approx(1.0)
    assert r["mae_r"] == pytest.approx(0.25)
    assert r["bars_held"] == 2


def test_replay_requires_complete_post_entry_bar():
    df = _bars([{"open":100,"high":110,"low":90,"close":100,"atr14":1.0}])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r == {"replay_valid": False, "failure_reason": "no_complete_post_entry_bars"}


def test_replay_preserves_right_tail_without_fixed_take_profit():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":100,"high":101,"low":99.5,"close":100.8,"atr14":1.0},
        {"open":100.8,"high":102,"low":100.4,"close":101.8,"atr14":1.0},
        {"open":101.8,"high":104,"low":101.5,"close":103.8,"atr14":1.0},
        {"open":103.8,"high":106,"low":103.4,"close":105.8,"atr14":1.0},
        {"open":105.8,"high":108,"low":105.2,"close":107.5,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r["replay_valid"] is True
    assert r["realized_r"] > 2.0
    assert r["exit_reason"] == "time"


def test_replay_profit_lock_prevents_large_winner_round_trip():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":100,"high":101,"low":99.5,"close":100.8,"atr14":1.0},
        {"open":100.8,"high":104,"low":100.5,"close":103.8,"atr14":1.0},
        {"open":103.8,"high":104.2,"low":100.0,"close":100.5,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r["exit_reason"] == "adaptive_stop"
    assert r["realized_r"] > 0


def test_replay_gap_through_stop_uses_gap_fill():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":97,"high":97.5,"low":96.5,"close":97,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r["exit_price"] == 97
    assert r["realized_r"] == pytest.approx(-1.5)


def test_replay_short_runner_is_symmetric():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":100,"high":100.5,"low":99,"close":99.2,"atr14":1.0},
        {"open":99.2,"high":99.5,"low":97,"close":97.2,"atr14":1.0},
        {"open":97.2,"high":97.5,"low":95,"close":95.2,"atr14":1.0},
        {"open":95.2,"high":95.5,"low":93,"close":93.2,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="short", entry_price=100, initial_stop=102)
    assert r["replay_valid"] is True
    assert r["realized_r"] > 2.0
