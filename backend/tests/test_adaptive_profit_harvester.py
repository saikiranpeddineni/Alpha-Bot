import pytest

from app.analytics.adaptive_profit_harvester import HarvestConfig, adaptive_stop


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
    second = adaptive_stop(
        direction="long", entry_price=100, initial_stop=98, prior_peak=104.5, atr=1,
        current_stop=first.stop_price,
    )
    assert second.stop_price >= first.stop_price


def test_stop_is_monotonic_for_short():
    first = adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=96, atr=1)
    second = adaptive_stop(
        direction="short", entry_price=100, initial_stop=102, prior_peak=95.5, atr=1,
        current_stop=first.stop_price,
    )
    assert second.stop_price <= first.stop_price


def test_never_widens_original_risk_long_or_short():
    long_d = adaptive_stop(direction="long", entry_price=100, initial_stop=98, prior_peak=100.5, atr=3)
    short_d = adaptive_stop(direction="short", entry_price=100, initial_stop=102, prior_peak=99.5, atr=3)
    assert long_d.stop_price >= 98
    assert short_d.stop_price <= 102


def test_invalid_zero_risk_is_rejected():
    with pytest.raises(ValueError):
        adaptive_stop(direction="long", entry_price=100, initial_stop=100, prior_peak=101, atr=1)
