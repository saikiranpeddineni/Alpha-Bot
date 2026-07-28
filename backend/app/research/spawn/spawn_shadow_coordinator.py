"""Out-of-band coordinator for the bounded SHADOW-ONLY spawn laboratory."""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from app.core.logging import applog
from app.db.database import Database, SpawnShadowEventRow
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor

class SpawnShadowCoordinator:
    """Autonomous research lifecycle with a structural zero-broker boundary."""
    def __init__(self, db: Database | None = None, max_queue_size: int = 1000, evolution_settings: EvolutionSettings | None = None, evolution_interval: int = 250) -> None:
        self.db=db; self.max_queue_size=max_queue_size; self.executors={}; self.queue=asyncio.Queue(maxsize=max_queue_size); self.worker_task=None; self.running=False; self.shadow_events_dropped=0; self.shutdown_undrained=0; self.worker_errors=0; self.observations_processed=0; self.candidate_observations_processed=0; self.market_bars_processed=0; self.evolution_interval=max(1,int(evolution_interval)); self.evolution=SpawnEvolutionEngine(evolution_settings); self.children_spawned=0; self.children_retired=0; self.evolution_cycles=0
    def telemetry(self)->dict[str,Any]:
        return {"shadow_events_dropped":self.shadow_events_dropped,"shutdown_undrained":self.shutdown_undrained,"worker_errors":self.worker_errors,"queue_size":self.queue.qsize(),"executors_count":len(self.executors),"active_children":sum(executor.status=="ACTIVE" for executor in self.executors.values()),"running":self.running,"observations_processed":self.observations_processed,"candidate_observations_processed":self.candidate_observations_processed,"market_bars_processed":self.market_bars_processed,"evolution_cycles":self.evolution_cycles,"children_spawned":self.children_spawned,"children_retired":self.children_retired,"generation":self.evolution.generation}
    @staticmethod
    def _generation_from_config(config:ChildPolicyConfig)->int:
        match=re.fullmatch(r"g(\d+)",str(config.policy_version or "").lower())
        return int(match.group(1)) if match else 0
    def initialize_from_db(self)->int:
        if not self.db:return 0
        count=0; max_generation=self.evolution.generation
        try:
            for row in self.db.get_active_spawned_strategies():
                policy_id=row["policy_id"]; raw=row.get("config_snapshot_json"); expected=row.get("config_fingerprint")
                if not raw:applog.error("spawn_coordinator",f"Policy {policy_id} disabled: MISSING_CONFIG_SNAPSHOT"); continue
                try:config=ChildPolicyConfig.from_dict(json.loads(raw))
                except Exception as exc:applog.error("spawn_coordinator",f"Policy {policy_id} disabled: INVALID_CONFIG_SNAPSHOT:{exc}"); continue
                max_generation=max(max_generation,self._generation_from_config(config))
                executor=ChildPolicyExecutor(config)
                if expected and expected!=config.compute_fingerprint():executor.status="UNAVAILABLE"; executor.unavailable_reason="FINGERPRINT_MISMATCH"; applog.error("spawn_coordinator",f"Policy {policy_id} disabled: FINGERPRINT_MISMATCH")
                elif executor.status=="ACTIVE":
                    checkpoint=self.db.get_spawn_portfolio_checkpoint(policy_id)
                    if checkpoint:
                        try:executor.portfolio.restore_from_checkpoint(checkpoint)
                        except Exception as exc:executor.status="UNAVAILABLE"; executor.unavailable_reason=f"CORRUPT_CHECKPOINT:{exc}"; applog.error("spawn_coordinator",f"Policy {policy_id} checkpoint rejected: {exc}")
                self.executors[policy_id]=executor
                if executor.status=="ACTIVE":count+=1
            # Generation is durable provenance. Restart must continue from the highest
            # persisted generation rather than silently returning to g1.
            self.evolution.generation=max_generation
        except Exception as exc:applog.error("spawn_coordinator",f"Research initialization failed: {exc}")
        return count
    def _bootstrap_if_empty(self)->None:
        if any(executor.status=="ACTIVE" for executor in self.executors.values()):return
        for config in self.evolution.bootstrap_configs():
            try:self.add_child(config,persist=bool(self.db))
            except Exception as exc:applog.warning("spawn_coordinator",f"Bootstrap child skipped: {exc}")
    async def start(self)->None:
        if self.running:return
        self.running=True; self.initialize_from_db(); self._bootstrap_if_empty(); self.worker_task=asyncio.create_task(self._background_worker(),name="spawn-shadow-worker"); applog.info("spawn_coordinator","Shadow army worker started.")
    async def stop(self)->None:
        if not self.running:return
        try:await asyncio.wait_for(self.queue.join(),timeout=3.0)
        except asyncio.TimeoutError:self.shutdown_undrained+=self.queue.qsize(); applog.warning("spawn_coordinator",f"Shutdown with {self.queue.qsize()} undrained spawn observations")
        self.running=False
        if self.worker_task:
            self.worker_task.cancel()
            try:await self.worker_task
            except asyncio.CancelledError:pass
            self.worker_task=None
    def publish_observation(self,observation:dict[str,Any])->bool:
        if not self.running or not self.executors:return False
        try:self.queue.put_nowait(observation); return True
        except asyncio.QueueFull:self.shadow_events_dropped+=1; return False
        except Exception as exc:self.shadow_events_dropped+=1; applog.warning("spawn_coordinator",f"Shadow publish failed: {exc}"); return False
    def publish_market_bar(self,symbol:str,open_px:float,high_px:float,low_px:float,close_px:float,ts:datetime)->bool:
        return self.publish_observation({"observation_type":"MARKET_BAR","symbol":str(symbol).upper(),"open":open_px,"high":high_px,"low":low_px,"close":close_px,"ts":ts})
    async def _background_worker(self)->None:
        while self.running:
            try:
                observation=await self.queue.get()
                try:await self._process_observation_out_of_band(observation)
                finally:self.queue.task_done()
            except asyncio.CancelledError:break
            except Exception as exc:self.worker_errors+=1; applog.error("spawn_coordinator",f"Shadow worker error: {exc}")
    @staticmethod
    def _candidate_id(observation:dict[str,Any])->str:
        candidate=observation.get("champion_candidate") or {}; ts=observation.get("ts"); symbol=str(observation.get("symbol","")).upper(); strategy=observation.get("strategy","momentum"); return candidate.get("id") or f"{symbol}_{str(strategy).upper()}_{int(ts.timestamp())}"
    @classmethod
    def _event_id(cls,policy_id:str,observation:dict[str,Any])->str:
        ts=observation["ts"]; candidate_id=cls._candidate_id(observation); return hashlib.sha256(f"{policy_id}|{candidate_id}|{ts.isoformat()}|EVALUATION".encode()).hexdigest()
    def _event_exists(self,event_id:str)->bool:
        if not self.db:return False
        with self.db._session() as session:return session.get(SpawnShadowEventRow,event_id) is not None
    @staticmethod
    def _bar_exit_event(policy_id:str,executor:ChildPolicyExecutor,observation:dict[str,Any],trade:dict[str,Any])->dict[str,Any]:
        ts=observation["ts"]; symbol=str(observation["symbol"]).upper(); event_id=hashlib.sha256(f"{policy_id}|{symbol}|{ts.isoformat()}|BAR_EXIT".encode()).hexdigest()
        return {"id":event_id,"policy_id":policy_id,"parent_policy_id":executor.config.parent_policy_id,"config_fingerprint":executor.config_fingerprint,"candidate_id":f"bar_exit_{symbol}_{int(ts.timestamp())}","ts":ts,"symbol":symbol,"strategy":trade.get("strategy","market_bar"),"direction":trade["side"],"signal_generated":False,"hypothetical_final_decision":"NON_COMPARABLE","disagreement_category":"NON_COMPARABLE","entry_price":trade["entry_price"],"virtual_exit_price":trade["exit_price"],"virtual_exit_reason":trade["exit_reason"],"realized_r":trade["realized_r"],"mfe_r":trade["mfe_r"],"mae_r":trade["mae_r"],"payload_obj":{"exit_record":trade}}
    async def _process_observation_out_of_band(self,observation:dict[str,Any])->None:
        is_market_bar=observation.get("observation_type")=="MARKET_BAR"
        def work():
            events=[]; checkpoints=[]; symbol=str(observation.get("symbol","")).upper()
            for policy_id,executor in list(self.executors.items()):
                try:
                    if is_market_bar:
                        if executor.status!="ACTIVE" or symbol not in executor.portfolio.positions:continue
                        trade=executor.process_market_bar(observation); checkpoints.append((policy_id,executor.portfolio.to_checkpoint_dict()))
                        if trade:events.append(self._bar_exit_event(policy_id,executor,observation,trade))
                    else:
                        if self.db and self._event_exists(self._event_id(policy_id,observation)):continue
                        event=executor.evaluate_observation(observation)
                        if event:events.append(event); checkpoints.append((policy_id,executor.portfolio.to_checkpoint_dict()))
                except Exception as error:self.worker_errors+=1; applog.error("spawn_coordinator",f"Child {policy_id} evaluation failed: {error}")
            if self.db and(events or checkpoints):self.db.commit_spawn_observation(events,checkpoints)
        await asyncio.to_thread(work); self.observations_processed+=1
        if is_market_bar:self.market_bars_processed+=1; return
        self.candidate_observations_processed+=1
        if self.candidate_observations_processed%self.evolution_interval==0:await asyncio.to_thread(self._run_evolution_cycle_sync,observation.get("ts"))
    def add_child(self,config:ChildPolicyConfig,persist:bool=True)->bool:
        if config.execution_mode!="SHADOW_ONLY" or config.policy_id in self.executors:return False
        fingerprint=config.compute_fingerprint(); behavior_fp=self.evolution.behavior_config_fingerprint(config)
        # Identity fingerprints contain lineage. Evolution needs behavioral uniqueness,
        # otherwise the army can grow copies of the same policy under new IDs.
        if any(self.evolution.behavior_config_fingerprint(executor.config)==behavior_fp for executor in self.executors.values()):return False
        executor=ChildPolicyExecutor(config)
        if executor.status!="ACTIVE":return False
        if persist and self.db:self.db.save_spawned_strategy(policy_id=config.policy_id,seed_capital=config.seed_capital,parent_policy_id=config.parent_policy_id,policy_version=config.policy_version,config_snapshot_json=config.snapshot_json(),config_fingerprint=fingerprint,created_from_candidate=config.created_from_candidate,spawn_reason=config.spawn_reason,execution_mode="SHADOW_ONLY",code_commit_sha=config.code_commit_sha,model_manifest_sha=config.model_manifest_sha,spawned_at=config.spawned_at)
        self.executors[config.policy_id]=executor; self.children_spawned+=1; return True
    def _run_evolution_cycle_sync(self,now:datetime|None=None)->None:
        if not isinstance(now,datetime) or now.tzinfo is None:now=datetime.now(timezone.utc)
        self.evolution_cycles+=1
        # Learn while parent and child are still in their evaluated lifecycle state.
        # Retiring a parent first would apply the ineligible fitness sentinel and poison
        # the child's mutation delta with a false ~+1,000,000 advantage.
        self.evolution.learn_mutation_outcomes(self.executors)
        for policy_id in self.evolution.retirement_candidates(self.executors):
            try:self.retire_child(policy_id,now)
            except Exception as exc:self.worker_errors+=1; applog.error("spawn_coordinator",f"Retirement failed for {policy_id}: {exc}")
        for child in self.evolution.next_generation(self.executors,now):
            try:self.add_child(child,persist=bool(self.db))
            except ValueError as exc:applog.warning("spawn_coordinator",f"Spawn blocked by safety ceiling: {exc}")
            except Exception as exc:self.worker_errors+=1; applog.error("spawn_coordinator",f"Spawn failed closed: {exc}")
    def retire_child(self,policy_id:str,now:datetime|None=None)->bool:
        executor=self.executors.get(policy_id); now_ts=now or datetime.now(timezone.utc)
        if not executor or executor.status!="ACTIVE":return False
        executor.status="RETIRED"; events=[]
        for symbol in list(executor.portfolio.positions):
            position=executor.portfolio.positions[symbol]; price=float(executor.portfolio.last_prices.get(symbol,position.entry_price)); trade=executor.portfolio.close_virtual_position(symbol,price,"RETIRED_FLATTEN",now_ts); event_id=hashlib.sha256(f"{policy_id}|{symbol}|{now_ts.isoformat()}|RETIREMENT_EXIT".encode()).hexdigest(); events.append({"id":event_id,"policy_id":policy_id,"parent_policy_id":executor.config.parent_policy_id,"config_fingerprint":executor.config_fingerprint,"candidate_id":f"retire_{symbol}_{int(now_ts.timestamp())}","ts":now_ts,"symbol":symbol,"strategy":position.strategy,"direction":position.side,"signal_generated":False,"hypothetical_final_decision":"REJECTED","disagreement_category":"NON_COMPARABLE","entry_price":position.entry_price,"virtual_exit_price":trade["exit_price"],"virtual_exit_reason":"RETIRED_FLATTEN","realized_r":trade["realized_r"],"mfe_r":trade["mfe_r"],"mae_r":trade["mae_r"],"payload_obj":{"exit_record":trade}})
        if self.db:self.db.retire_spawned_strategy(policy_id); self.db.commit_spawn_observation(events,[(policy_id,executor.portfolio.to_checkpoint_dict())])
        self.children_retired+=1; return True
