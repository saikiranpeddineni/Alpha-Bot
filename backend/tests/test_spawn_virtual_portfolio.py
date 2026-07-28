"""Unit tests for Spawn Virtual Portfolio accounting, determinism, and checkpointing."""
import pytest
from datetime import datetime, timezone, timedelta

from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio, VirtualPosition


def test_virtual_portfolio_initialization_and_sizing():
    vp=SpawnVirtualPortfolio(policy_id="spawn_g0_seed_01",seed_capital=10000.0,risk_config={"risk_per_trade_pct":1.0,"max_position_pct":25.0,"max_open_positions":2,"allow_fractional":False})
    assert vp.cash==10000.0; assert vp.can_open_position("AAPL") is True
    qty=vp.calculate_position_size(price=150.0,stop=148.50); assert qty==16.0


def test_watermark_determinism():
    vp=SpawnVirtualPortfolio("spawn_g0_seed_01",seed_capital=10000.0); now=datetime.now(timezone.utc)
    vp.open_virtual_position("AAPL","long",qty=10.0,price=150.0,stop=145.0,target=160.0,now=now); assert "AAPL" in vp.positions
    res1=vp.update_ohlc_and_check_exits("AAPL",o=150.0,h=151.0,l=148.0,c=149.0,now=now); assert res1 is None
    res_dup=vp.update_ohlc_and_check_exits("AAPL",o=150.0,h=151.0,l=140.0,c=140.0,now=now); assert res_dup is None


def test_virtual_position_lifecycle():
    vp=SpawnVirtualPortfolio("spawn_g0_seed_01",seed_capital=10000.0); now=datetime.now(timezone.utc)
    pos=vp.open_virtual_position("MSFT","long",qty=20.0,price=300.0,stop=290.0,target=320.0,now=now); assert pos.symbol=="MSFT"; assert pos.entry_notional==6000.0
    mtm=vp.mark_to_market({"MSFT":310.0}); assert mtm==10200.0
    exit_rec=vp.update_ohlc_and_check_exits("MSFT",o=310.0,h=321.0,l=310.0,c=320.0,now=now+timedelta(minutes=1))
    assert exit_rec is not None; assert exit_rec["exit_reason"]=="TAKE_PROFIT"; assert exit_rec["pnl"]==400.0; assert vp.cash==10400.0; assert "MSFT" not in vp.positions


def test_gap_through_stop_uses_open_not_stop_price():
    vp=SpawnVirtualPortfolio("seed",seed_capital=10000.0); now=datetime.now(timezone.utc)
    vp.open_virtual_position("NVDA","long",10,100.0,99.0,110.0,now)
    rec=vp.update_ohlc_and_check_exits("NVDA",o=97.0,h=98.0,l=96.0,c=97.5,now=now+timedelta(minutes=1))
    assert rec["exit_reason"]=="STOP_LOSS_GAP"; assert rec["exit_price"]==97.0; assert rec["realized_r"]==pytest.approx(-3.0)


def test_exit_bar_is_included_in_mfe_mae():
    vp=SpawnVirtualPortfolio("seed",seed_capital=10000.0); now=datetime.now(timezone.utc)
    vp.open_virtual_position("NVDA","long",10,100.0,95.0,110.0,now)
    rec=vp.update_ohlc_and_check_exits("NVDA",o=100.0,h=111.0,l=99.0,c=110.0,now=now+timedelta(minutes=1))
    assert rec["mfe_r"]==pytest.approx(2.2); assert rec["mae_r"]==pytest.approx(0.2)


def test_invalid_ohlc_is_rejected_without_mutating_position():
    vp=SpawnVirtualPortfolio("seed",seed_capital=10000.0); now=datetime.now(timezone.utc)
    vp.open_virtual_position("NVDA","long",10,100.0,95.0,110.0,now)
    with pytest.raises(ValueError):vp.update_ohlc_and_check_exits("NVDA",o=100.0,h=99.0,l=98.0,c=100.0,now=now+timedelta(minutes=1))
    assert "NVDA" in vp.positions


def test_checkpoint_serialization_and_restore_preserves_strategy():
    vp=SpawnVirtualPortfolio("spawn_g0_seed_01",seed_capital=10000.0); now=datetime.now(timezone.utc)
    vp.open_virtual_position("NVDA","long",qty=15.0,price=100.0,stop=95.0,target=110.0,now=now,candidate_obs={"strategy":"momentum"})
    checkpoint=vp.to_checkpoint_dict(); assert checkpoint["policy_id"]=="spawn_g0_seed_01"; assert "NVDA" in checkpoint["positions"]
    vp2=SpawnVirtualPortfolio("spawn_g0_seed_01",seed_capital=10000.0); vp2.restore_from_checkpoint(checkpoint)
    assert "NVDA" in vp2.positions; assert vp2.positions["NVDA"].qty==15.0; assert vp2.positions["NVDA"].entry_price==100.0; assert vp2.positions["NVDA"].strategy=="momentum"
