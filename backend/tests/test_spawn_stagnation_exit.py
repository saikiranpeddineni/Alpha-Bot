from datetime import datetime, timedelta, timezone
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


def portfolio(**overrides):
    cfg={"stagnation_exit_minutes":30,"stagnation_max_abs_r":0.20}; cfg.update(overrides)
    return SpawnVirtualPortfolio("stagnation",10000,cfg)


def test_disabled_by_default_preserves_existing_behavior():
    p=SpawnVirtualPortfolio("baseline",10000,{})
    t=datetime(2026,7,24,14,0,tzinfo=timezone.utc); p.open_virtual_position("MU","short",10,100,102,90,t)
    assert p.update_ohlc_and_check_exits("MU",100,100.5,99.5,100.1,t+timedelta(hours=2)) is None
    assert "MU" in p.positions


def test_stagnant_position_closes_at_completed_bar_close():
    p=portfolio(); t=datetime(2026,7,24,14,0,tzinfo=timezone.utc); p.open_virtual_position("MU","short",10,100,102,90,t)
    out=p.update_ohlc_and_check_exits("MU",100,100.3,99.8,100.2,t+timedelta(minutes=30))
    assert out["exit_reason"]=="STAGNATION_EXIT"
    assert out["exit_price"]==100.2


def test_directional_progress_prevents_stagnation_exit():
    p=portfolio(); t=datetime(2026,7,24,14,0,tzinfo=timezone.utc); p.open_virtual_position("MU","short",10,100,102,90,t)
    assert p.update_ohlc_and_check_exits("MU",99.4,99.5,99.0,99.4,t+timedelta(minutes=30)) is None
    assert "MU" in p.positions


def test_stop_and_target_have_priority_over_stagnation():
    p=portfolio(); t=datetime(2026,7,24,14,0,tzinfo=timezone.utc); p.open_virtual_position("MU","short",10,100,102,90,t)
    out=p.update_ohlc_and_check_exits("MU",101,102.1,99.8,100.1,t+timedelta(minutes=30))
    assert out["exit_reason"]=="STOP_LOSS"


def test_duplicate_bar_cannot_double_close():
    p=portfolio(); t=datetime(2026,7,24,14,0,tzinfo=timezone.utc); p.open_virtual_position("MU","short",10,100,102,90,t); ts=t+timedelta(minutes=30)
    first=p.update_ohlc_and_check_exits("MU",100,100.3,99.8,100.2,ts); second=p.update_ohlc_and_check_exits("MU",100,100.3,99.8,100.2,ts)
    assert first is not None and second is None and p.trade_count==1
