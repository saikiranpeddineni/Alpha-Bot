from datetime import UTC, datetime, timedelta

import pytest

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


def _executor(**risk):
    now = datetime(2026, 7, 29, 14, 30, tzinfo=UTC)
    cfg = ChildPolicyConfig(policy_id="spawn_test_child", spawned_at=now - timedelta(days=1), risk_overrides=risk)
    return ChildPolicyExecutor(cfg), now


def _obs(now, **overrides):
    obs = {"symbol":"SPY","strategy":"momentum","direction":"long","price":100.0,"stop_loss":99.0,"take_profit":102.0,"ts":now,"champion_candidate":{"id":"cand-spy-1","ml_probability":0.70,"meta_probability":0.70,"trade_score":80.0,"final_decision":"accepted"}}
    obs.update(overrides); return obs


def test_missing_stop_is_not_synthesized_and_cannot_open():
    executor, now = _executor(); obs = _obs(now); obs.pop("stop_loss"); record = executor.evaluate_observation(obs)
    assert record is not None; assert record["stop_loss"] is None; assert record["risk_decision"] == "REJECT_RISK_STOP_UNAVAILABLE"; assert record["hypothetical_final_decision"] == "REJECTED"; assert "SPY" not in executor.portfolio.positions


def test_missing_meta_evidence_cannot_be_inferred_from_production_final_decision():
    executor, now = _executor(); obs = _obs(now); obs["champion_candidate"].pop("meta_probability"); record = executor.evaluate_observation(obs)
    assert record["meta_decision"] == "UNAVAILABLE"; assert record["hypothetical_final_decision"] == "REJECTED"; assert "SPY" not in executor.portfolio.positions


def test_duplicate_symbol_cannot_rotate_out_unrelated_incumbent():
    executor, now = _executor(rotation_enabled=True, max_open_positions=2)
    executor.portfolio.open_virtual_position("SPY","long",1.0,100.0,99.0,102.0,now-timedelta(minutes=30)); executor.portfolio.open_virtual_position("QQQ","long",1.0,200.0,198.0,204.0,now-timedelta(minutes=30))
    record=executor.evaluate_observation(_obs(now)); assert record is None; assert set(executor.portfolio.positions)=={"SPY","QQQ"}


def test_disagreement_is_computed_after_final_execution_outcome():
    executor, now = _executor(max_open_positions=0); record=executor.evaluate_observation(_obs(now)); assert record["hypothetical_final_decision"]=="REJECTED"; assert record["disagreement_category"]=="CHAMPION_ONLY"


def test_virtual_portfolio_rejects_nonfinite_or_directionally_invalid_entries():
    p=SpawnVirtualPortfolio("p"); now=datetime(2026,7,29,14,30,tzinfo=UTC)
    assert p.open_virtual_position("SPY","long",float("nan"),100,99,102,now) is None
    assert p.open_virtual_position("SPY","long",1,100,101,102,now) is None
    assert p.open_virtual_position("SPY","long",1,100,99,98,now) is None
    assert p.positions=={}


def test_market_bar_before_position_entry_is_rejected_without_mutation():
    p=SpawnVirtualPortfolio("p"); entry=datetime(2026,7,29,14,30,tzinfo=UTC); p.open_virtual_position("SPY","long",1,100,99,102,entry)
    with pytest.raises(ValueError,match="before position entry"):p.update_ohlc_and_check_exits("SPY",100,103,98,101,entry-timedelta(minutes=1))
    assert "SPY" in p.positions; assert p.trade_count==0; assert not p.processed_watermarks


def test_close_before_entry_is_fail_closed_and_preserves_position():
    p=SpawnVirtualPortfolio("p"); entry=datetime(2026,7,29,14,30,tzinfo=UTC); p.open_virtual_position("SPY","long",1,100,99,102,entry)
    assert p.close_virtual_position("SPY",101,"BAD_TIME",entry-timedelta(seconds=1)) is None
    assert "SPY" in p.positions; assert p.trade_count==0
