import pandas as pd
import pytest

from app.analytics.adaptive_profit_harvester import HarvestConfig, adaptive_stop, replay_adaptive_harvest


def _bars(rows):
    return pd.DataFrame(rows, index=pd.date_range("2026-07-23 10:00", periods=len(rows), freq="min"))


def test_v2_does_not_tighten_on_mfe_alone():
    d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=104,
                      prior_close=103.8, atr=1)
    assert d.state == "runner_preserve"
    assert d.stop_price == 98


def test_v2_does_not_repeat_v1_075r_breakeven_choke():
    d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=101.8,
                      prior_close=101.2, atr=1)
    assert d.stop_price == 98
    assert d.locked_r == pytest.approx(-1.0)


def test_v2_requires_confirmed_giveback_before_locking():
    quiet = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=104,
                          prior_close=103.0, atr=1)
    deteriorated = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=104,
                                 prior_close=102.4, atr=1)
    assert quiet.state == "runner_preserve"
    assert quiet.stop_price == 98
    assert deteriorated.state == "confirmed_giveback"
    assert deteriorated.stop_price > 98


def test_v2_runner_needs_larger_giveback_and_gets_wider_atr_room():
    cfg = HarvestConfig()
    d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=106,
                      prior_close=103.8, atr=1, config=cfg)
    assert d.state == "runner_giveback"
    assert cfg.runner_atr_mult > cfg.lock_atr_mult
    assert d.locked_r >= cfg.runner_floor_r


def test_v2_exceptional_runner_protection_is_delayed_and_material():
    cfg = HarvestConfig()
    untouched = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=109,
                              prior_close=107.0, atr=1, config=cfg)
    protected = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=109,
                              prior_close=106.4, atr=1, config=cfg)
    assert untouched.state == "runner_preserve"
    assert protected.state == "exceptional_giveback"
    assert protected.locked_r >= cfg.exceptional_floor_r


def test_stop_is_monotonic_for_long_after_activation():
    first = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=104,
                          prior_close=102.4, atr=1)
    second = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=105,
                           prior_close=103.0, atr=1, current_stop=first.stop_price)
    assert second.stop_price >= first.stop_price


def test_stop_is_monotonic_for_short_after_activation():
    first = adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=96,
                          prior_close=97.6, atr=1)
    second = adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=95,
                           prior_close=97.0, atr=1, current_stop=first.stop_price)
    assert second.stop_price <= first.stop_price


def test_never_widens_original_risk_long_or_short():
    assert adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=101,
                         prior_close=100.5, atr=3).stop_price >= 98
    assert adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=99,
                         prior_close=99.5, atr=3).stop_price <= 102


def test_invalid_initial_risk_is_rejected():
    for side, stop in (("long", 100), ("long", 101), ("short", 100), ("short", 99)):
        with pytest.raises(ValueError):
            adaptive_stop(direction=side, entry_price=100, initial_stop=stop, prior_peak=101,
                          prior_close=100, atr=1)


def test_replay_rejects_wrong_side_initial_stop():
    df = _bars([
        {"open":100,"high":101,"low":99,"close":100,"atr14":1.0},
        {"open":100,"high":102,"low":99,"close":101,"atr14":1.0},
    ])
    assert replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=101)["replay_valid"] is False
    assert replay_adaptive_harvest(df, entry_idx=0, direction="short", entry_price=100, initial_stop=99)["replay_valid"] is False


def test_replay_excludes_ambiguous_entry_bar_extremes():
    df = _bars([
        {"open":95,"high":110,"low":90,"close":100,"atr14":1.0},
        {"open":100,"high":101,"low":99.5,"close":100.8,"atr14":1.0},
        {"open":100.8,"high":102,"low":100.4,"close":101.8,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r["mfe_r"] == pytest.approx(1.0)
    assert r["mae_r"] == pytest.approx(0.25)
    assert r["bars_held"] == 2


def test_replay_requires_complete_post_entry_bar():
    df = _bars([{"open":100,"high":110,"low":90,"close":100,"atr14":1.0}])
    assert replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)["replay_valid"] is False


def test_v2_preserves_clean_right_tail_runner():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":100,"high":101,"low":99.5,"close":100.8,"atr14":1.0},
        {"open":100.8,"high":102,"low":100.4,"close":101.8,"atr14":1.0},
        {"open":101.8,"high":104,"low":101.5,"close":103.8,"atr14":1.0},
        {"open":103.8,"high":106,"low":103.4,"close":105.8,"atr14":1.0},
        {"open":105.8,"high":108,"low":105.2,"close":107.5,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="long", entry_price=100, initial_stop=98)
    assert r["policy_version"] == "v2_giveback_confirmed"
    assert r["realized_r"] > 3.0
    assert r["exit_reason"] == "time"


def test_v2_harvests_only_after_completed_bar_confirms_giveback():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":100,"high":104,"low":99.8,"close":103.8,"atr14":1.0},
        {"open":103.8,"high":104.2,"low":102.2,"close":102.3,"atr14":1.0},
        {"open":102.3,"high":102.5,"low":99.5,"close":100,"atr14":1.0},
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


def test_v2_short_runner_is_symmetric_and_preserved():
    df = _bars([
        {"open":100,"high":100.2,"low":99.8,"close":100,"atr14":1.0},
        {"open":100,"high":100.5,"low":99,"close":99.2,"atr14":1.0},
        {"open":99.2,"high":99.5,"low":97,"close":97.2,"atr14":1.0},
        {"open":97.2,"high":97.5,"low":95,"close":95.2,"atr14":1.0},
        {"open":95.2,"high":95.5,"low":93,"close":93.2,"atr14":1.0},
    ])
    r = replay_adaptive_harvest(df, entry_idx=0, direction="short", entry_price=100, initial_stop=102)
    assert r["realized_r"] > 3.0
