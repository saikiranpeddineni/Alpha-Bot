"""Correctness tests for bounded autonomous shadow evolution."""
from datetime import datetime, timezone, timedelta
import pytest
from app.db.database import Database
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator


def passing_obs(ts, cid="c1", price=100.0):
    return {"symbol":"AAPL","strategy":"momentum","direction":"long","price":price,"open":price,"high":price+0.2,"low":price-0.2,"close":price,
            "atr":1.0,"ts":ts,"stop_loss":95.0,"take_profit":110.0,
            "champion_candidate":{"id":cid,"ml_probability":0.70,"meta_probability":0.75,"trade_score":85.0,"final_decision":"accepted"}}


def test_config_snapshot_round_trip_preserves_restart_timestamp_and_fingerprint():
    now=datetime.now(timezone.utc); cfg=ChildPolicyConfig(policy_id="roundtrip",spawned_at=now,ml_overrides={"ml_threshold":.51})
    restored=ChildPolicyConfig.from_dict(__import__("json").loads(cfg.snapshot_json()))
    assert restored.spawned_at==now
    assert restored.compute_fingerprint()==cfg.compute_fingerprint()
    assert restored.execution_mode=="SHADOW_ONLY"


def test_non_shadow_execution_mode_is_structurally_rejected():
    with pytest.raises(ValueError):ChildPolicyConfig(policy_id="bad",execution_mode="LIVE")


def test_bootstrap_population_is_bounded_diverse_and_shadow_only():
    evo=SpawnEvolutionEngine(EvolutionSettings(bootstrap_population=6,max_population=12,elite_count=3))
    cohort=evo.bootstrap_configs(datetime.now(timezone.utc))
    assert len(cohort)==6
    assert len({c.compute_fingerprint() for c in cohort})==6
    assert all(c.execution_mode=="SHADOW_ONLY" for c in cohort)


def test_selection_requires_minimum_sample_and_positive_quality():
    settings=EvolutionSettings(max_population=10,elite_count=2,min_trades_for_selection=5)
    evo=SpawnEvolutionEngine(settings); now=datetime.now(timezone.utc)
    ex=ChildPolicyExecutor(ChildPolicyConfig(policy_id="tiny",spawned_at=now-timedelta(days=1)))
    ex.portfolio.realized_r=10.0; ex.portfolio.trade_count=1
    assert evo.select_elites({"tiny":ex})==[]


def test_fitness_uses_historical_max_drawdown_not_current_drawdown():
    settings=EvolutionSettings(max_population=10,elite_count=2,min_trades_for_selection=1,max_drawdown_pct=5.0)
    evo=SpawnEvolutionEngine(settings); now=datetime.now(timezone.utc)
    ex=ChildPolicyExecutor(ChildPolicyConfig(policy_id="dd",spawned_at=now-timedelta(days=1)))
    ex.portfolio.trade_count=3; ex.portfolio.realized_r=3.0
    ex.portfolio.max_drawdown_pct=7.5
    ex.portfolio.marked_equity=ex.portfolio.peak_equity
    assert ex.portfolio.get_drawdown_pct()==0.0
    score=evo.score(ex)
    assert score.drawdown_pct==7.5
    assert score.eligible is False


def test_next_generation_is_bounded_and_has_parent_lineage():
    settings=EvolutionSettings(max_population=4,elite_count=1,children_per_cycle=3,min_trades_for_selection=2)
    evo=SpawnEvolutionEngine(settings); now=datetime.now(timezone.utc)
    parent=ChildPolicyExecutor(ChildPolicyConfig(policy_id="elite",spawned_at=now-timedelta(days=1),seed_capital=2500.0))
    parent.portfolio.trade_count=10; parent.portfolio.realized_r=5.0
    children=evo.next_generation({"elite":parent},now)
    assert len(children)==3
    assert all(c.parent_policy_id=="elite" and c.execution_mode=="SHADOW_ONLY" for c in children)
    assert len({c.compute_fingerprint() for c in children})==3


@pytest.mark.asyncio
async def test_coordinator_bootstraps_when_database_has_no_children(tmp_path):
    db=Database(tmp_path/"army.db"); coord=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=3,max_population=8,elite_count=2))
    await coord.start()
    try:
        assert coord.telemetry()["active_children"]==3
        assert db.get_active_spawned_capital()==7500.0
    finally:await coord.stop()


@pytest.mark.asyncio
async def test_persisted_event_prevents_restart_double_mutation(tmp_path):
    db=Database(tmp_path/"idem.db"); now=datetime.now(timezone.utc)
    cfg=ChildPolicyConfig(policy_id="idem",spawned_at=now-timedelta(minutes=1),seed_capital=2500.0,ml_overrides={"ml_threshold":.48},meta_overrides={"meta_threshold":.55},risk_overrides={"min_trade_score":50.0})
    coord=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=0,max_population=4,elite_count=1))
    assert coord.add_child(cfg,persist=True)
    obs=passing_obs(now)
    await coord._process_observation_out_of_band(obs)
    first_qty=coord.executors["idem"].portfolio.positions["AAPL"].qty
    first_cash=coord.executors["idem"].portfolio.cash
    coord2=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=0,max_population=4,elite_count=1))
    assert coord2.initialize_from_db()==1
    await coord2._process_observation_out_of_band(obs)
    assert coord2.executors["idem"].portfolio.positions["AAPL"].qty==first_qty
    assert coord2.executors["idem"].portfolio.cash==first_cash


@pytest.mark.asyncio
async def test_market_bar_advances_open_position_without_triggering_evolution(tmp_path):
    db=Database(tmp_path/"bars.db"); now=datetime.now(timezone.utc)
    cfg=ChildPolicyConfig(policy_id="bar_child",spawned_at=now-timedelta(minutes=1),seed_capital=2500.0)
    coord=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=0,max_population=4,elite_count=1),evolution_interval=1)
    assert coord.add_child(cfg,persist=True)
    port=coord.executors["bar_child"].portfolio
    port.open_virtual_position("AAPL","long",10,100,95,110,now,"v1")
    bar={"observation_type":"MARKET_BAR","symbol":"AAPL","open":100.0,"high":101.0,"low":99.0,"close":100.5,"ts":now+timedelta(minutes=1)}
    await coord._process_observation_out_of_band(bar)
    assert port.last_prices["AAPL"]==100.5
    assert coord.market_bars_processed==1
    assert coord.candidate_observations_processed==0
    assert coord.evolution_cycles==0


@pytest.mark.asyncio
async def test_market_bar_exit_is_durable_and_restart_idempotent(tmp_path):
    db=Database(tmp_path/"bar_exit.db"); now=datetime.now(timezone.utc)
    cfg=ChildPolicyConfig(policy_id="bar_exit",spawned_at=now-timedelta(minutes=1),seed_capital=2500.0)
    coord=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=0,max_population=4,elite_count=1))
    assert coord.add_child(cfg,persist=True)
    coord.executors["bar_exit"].portfolio.open_virtual_position("AAPL","long",10,100,95,110,now,"v1")
    bar={"observation_type":"MARKET_BAR","symbol":"AAPL","open":100.0,"high":101.0,"low":94.0,"close":96.0,"ts":now+timedelta(minutes=1)}
    await coord._process_observation_out_of_band(bar)
    assert coord.executors["bar_exit"].portfolio.trade_count==1
    coord2=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=0,max_population=4,elite_count=1))
    assert coord2.initialize_from_db()==1
    await coord2._process_observation_out_of_band(bar)
    assert coord2.executors["bar_exit"].portfolio.trade_count==1


def test_retirement_uses_last_trusted_mark_not_entry_price(tmp_path):
    db=Database(tmp_path/"retire.db"); now=datetime.now(timezone.utc); coord=SpawnShadowCoordinator(db=db,evolution_settings=EvolutionSettings(bootstrap_population=0,max_population=4,elite_count=1))
    cfg=ChildPolicyConfig(policy_id="ret",spawned_at=now-timedelta(minutes=1),seed_capital=2500.0); assert coord.add_child(cfg,persist=True)
    p=coord.executors["ret"].portfolio; p.open_virtual_position("AAPL","long",10,100,95,120,now,"v1"); p.mark_to_market({"AAPL":105.0})
    assert coord.retire_child("ret",now+timedelta(minutes=1))
    assert p.closed_trades[-1]["exit_price"]==105.0
    assert p.closed_trades[-1]["pnl"]==50.0


def test_weak_policy_retired_only_after_sample_floor():
    settings=EvolutionSettings(max_population=6,elite_count=2,min_trades_for_selection=5); evo=SpawnEvolutionEngine(settings); now=datetime.now(timezone.utc)
    ex=ChildPolicyExecutor(ChildPolicyConfig(policy_id="weak",spawned_at=now-timedelta(days=1))); ex.portfolio.realized_r=-4.0; ex.portfolio.trade_count=4
    assert evo.retirement_candidates({"weak":ex})==[]
    ex.portfolio.trade_count=5
    assert evo.retirement_candidates({"weak":ex})==["weak"]
