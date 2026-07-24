"""Out-of-band coordinator for the bounded SHADOW-ONLY spawn laboratory."""
from __future__ import annotations
import asyncio, hashlib, json
from datetime import datetime, timezone
from typing import Any
from app.core.logging import applog
from app.db.database import Database, SpawnShadowEventRow
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor

class SpawnShadowCoordinator:
    """Autonomous research lifecycle with a structural zero-broker boundary."""
    def __init__(self,db:Database|None=None,max_queue_size:int=1000,evolution_settings:EvolutionSettings|None=None,evolution_interval:int=250)->None:
        self.db=db; self.max_queue_size=max_queue_size; self.executors={}; self.queue=asyncio.Queue(maxsize=max_queue_size); self.worker_task=None; self.running=False
        self.shadow_events_dropped=0; self.observations_processed=0; self.evolution_interval=max(1,int(evolution_interval)); self.evolution=SpawnEvolutionEngine(evolution_settings)
        self.children_spawned=0; self.children_retired=0; self.evolution_cycles=0
    def telemetry(self)->dict[str,Any]:
        return {"shadow_events_dropped":self.shadow_events_dropped,"queue_size":self.queue.qsize(),"executors_count":len(self.executors),
                "active_children":sum(e.status=="ACTIVE" for e in self.executors.values()),"running":self.running,"observations_processed":self.observations_processed,
                "evolution_cycles":self.evolution_cycles,"children_spawned":self.children_spawned,"children_retired":self.children_retired,"generation":self.evolution.generation}
    def initialize_from_db(self)->int:
        if not self.db:return 0
        count=0
        try:
            for row in self.db.get_active_spawned_strategies():
                pid=row["policy_id"]; raw=row.get("config_snapshot_json"); expected=row.get("config_fingerprint")
                try:
                    if not raw:raise ValueError("MISSING_CONFIG_SNAPSHOT")
                    cfg=ChildPolicyConfig.from_dict(json.loads(raw))
                    if expected and expected!=cfg.compute_fingerprint():raise ValueError("FINGERPRINT_MISMATCH")
                except Exception as exc:
                    applog.error("spawn_coordinator",f"Policy {pid} disabled: {exc}"); continue
                ex=ChildPolicyExecutor(cfg)
                if ex.status=="ACTIVE":
                    chk=self.db.get_spawn_portfolio_checkpoint(pid)
                    if chk:
                        try:ex.portfolio.restore_from_checkpoint(chk)
                        except Exception as exc:
                            ex.status="UNAVAILABLE"; ex.unavailable_reason=f"CORRUPT_CHECKPOINT:{exc}"; applog.error("spawn_coordinator",f"Policy {pid} checkpoint rejected: {exc}")
                self.executors[pid]=ex
                if ex.status=="ACTIVE":count+=1
        except Exception as exc:applog.error("spawn_coordinator",f"Research initialization failed: {exc}")
        return count
    def _bootstrap_if_empty(self)->None:
        if any(e.status=="ACTIVE" for e in self.executors.values()):return
        for cfg in self.evolution.bootstrap_configs():
            try:self.add_child(cfg,persist=bool(self.db))
            except Exception as exc:applog.warning("spawn_coordinator",f"Bootstrap child skipped: {exc}")
    async def start(self)->None:
        if self.running:return
        self.running=True; self.initialize_from_db(); self._bootstrap_if_empty(); self.worker_task=asyncio.create_task(self._background_worker()); applog.info("spawn_coordinator","Shadow army worker started.")
    async def stop(self)->None:
        if not self.running:return
        self.running=False
        if self.worker_task:
            self.worker_task.cancel()
            try:await self.worker_task
            except asyncio.CancelledError:pass
    def publish_observation(self,observation:dict[str,Any])->bool:
        if not self.running or not self.executors:return False
        try:self.queue.put_nowait(observation); return True
        except asyncio.QueueFull:self.shadow_events_dropped+=1; return False
        except Exception as exc:applog.warning("spawn_coordinator",f"Shadow publish failed: {exc}"); return False
    def publish_market_bar(self,symbol:str,open_px:float,high_px:float,low_px:float,close_px:float,ts:datetime)->bool:
        """Publish a closed 1-minute OHLC bar so open child positions receive subsequent price updates."""
        if not self.running or not self.executors:return False
        bar_obs:dict[str,Any]={"kind":"bar","symbol":symbol.upper(),"open":open_px,"high":high_px,"low":low_px,"close":close_px,"price":close_px,"ts":ts}
        try:self.queue.put_nowait(bar_obs); return True
        except asyncio.QueueFull:self.shadow_events_dropped+=1; return False
        except Exception as exc:applog.warning("spawn_coordinator",f"Shadow bar publish failed: {exc}"); return False

    async def _background_worker(self)->None:
        while self.running:
            try:
                obs=await self.queue.get()
                try:await self._process_observation_out_of_band(obs)
                finally:self.queue.task_done()
            except asyncio.CancelledError:break
            except Exception as exc:applog.error("spawn_coordinator",f"Shadow worker error: {exc}")
    @staticmethod
    def _candidate_id(obs:dict[str,Any])->str:
        cand=obs.get("champion_candidate") or {}; ts=obs.get("ts"); sym=str(obs.get("symbol","")).upper(); strategy=obs.get("strategy","momentum")
        return cand.get("id") or f"{sym}_{str(strategy).upper()}_{int(ts.timestamp())}"
    @classmethod
    def _event_id(cls,policy_id:str,obs:dict[str,Any])->str:
        ts=obs["ts"]; cid=cls._candidate_id(obs); return hashlib.sha256(f"{policy_id}|{cid}|{ts.isoformat()}|EVALUATION".encode()).hexdigest()
    def _event_exists(self,event_id:str)->bool:
        if not self.db:return False
        with self.db._session() as s:return s.get(SpawnShadowEventRow,event_id) is not None
    async def _process_observation_out_of_band(self,obs:dict[str,Any])->None:
        kind=obs.get("kind","candidate")
        def work():
            events=[]; checkpoints=[]
            if kind=="bar":
                # Bar tick: update OHLC for open positions in every active child
                sym=str(obs.get("symbol","")).upper()
                o=float(obs.get("open",obs.get("price",0))); h=float(obs.get("high",obs.get("price",0)))
                l=float(obs.get("low",obs.get("price",0))); c=float(obs.get("close",obs.get("price",0))); ts=obs["ts"]
                for pid,ex in list(self.executors.items()):
                    if ex.status!="ACTIVE" or sym not in ex.portfolio.positions:continue
                    try:
                        rec=ex.portfolio.update_ohlc_and_check_exits(sym,o,h,l,c,ts)
                        checkpoints.append((pid,ex.portfolio.to_checkpoint_dict()))
                        if rec:
                            import hashlib
                            evt_id=hashlib.sha256(f"{pid}|{sym}|{ts.isoformat()}|BAR_EXIT".encode()).hexdigest()
                            events.append({"id":evt_id,"policy_id":pid,"parent_policy_id":ex.config.parent_policy_id,
                                "config_fingerprint":ex.config_fingerprint,"candidate_id":f"bar_{sym}_{int(ts.timestamp())}",
                                "ts":ts,"symbol":sym,"strategy":"bar","direction":"","signal_generated":False,
                                "hypothetical_final_decision":"REJECTED","disagreement_category":"NON_COMPARABLE",
                                "entry_price":rec.get("exit_price"),"virtual_exit_price":rec.get("exit_price"),
                                "virtual_exit_reason":rec.get("exit_reason"),"realized_r":rec.get("realized_r"),
                                "mfe_r":rec.get("mfe_r"),"mae_r":rec.get("mae_r"),"payload_obj":{"exit_record":rec}})
                    except Exception as err:applog.error("spawn_coordinator",f"Child {pid} bar update failed: {err}")
            else:
                # Candidate observation: full evaluation + virtual entry/exit via evaluate_observation()
                for pid,ex in list(self.executors.items()):
                    try:
                        if self.db and self._event_exists(self._event_id(pid,obs)):continue
                        evt=ex.evaluate_observation(obs)
                        if evt:events.append(evt); checkpoints.append((pid,ex.portfolio.to_checkpoint_dict()))
                    except Exception as err:applog.error("spawn_coordinator",f"Child {pid} evaluation failed: {err}")
            if self.db and (events or checkpoints):self.db.commit_spawn_observation(events,checkpoints)
        await asyncio.to_thread(work); self.observations_processed+=1
        if kind!="bar" and self.observations_processed%self.evolution_interval==0:
            await asyncio.to_thread(self._run_evolution_cycle_sync,obs.get("ts"))

    def add_child(self,config:ChildPolicyConfig,persist:bool=True)->bool:
        if config.execution_mode!="SHADOW_ONLY" or config.policy_id in self.executors:return False
        fp=config.compute_fingerprint()
        if any(e.config_fingerprint==fp for e in self.executors.values()):return False
        ex=ChildPolicyExecutor(config)
        if ex.status!="ACTIVE":return False
        if persist and self.db:self.db.save_spawned_strategy(policy_id=config.policy_id,seed_capital=config.seed_capital,parent_policy_id=config.parent_policy_id,
            policy_version=config.policy_version,config_snapshot_json=config.snapshot_json(),config_fingerprint=fp,created_from_candidate=config.created_from_candidate,
            spawn_reason=config.spawn_reason,execution_mode="SHADOW_ONLY",code_commit_sha=config.code_commit_sha,model_manifest_sha=config.model_manifest_sha,spawned_at=config.spawned_at)
        self.executors[config.policy_id]=ex; self.children_spawned+=1; return True
    def _run_evolution_cycle_sync(self,now:datetime|None=None)->None:
        if not isinstance(now,datetime) or now.tzinfo is None:now=datetime.now(timezone.utc)
        self.evolution_cycles+=1
        for pid in self.evolution.retirement_candidates(self.executors):
            try:self.retire_child(pid,now)
            except Exception as exc:applog.error("spawn_coordinator",f"Retirement failed for {pid}: {exc}")
        for child in self.evolution.next_generation(self.executors,now):
            try:self.add_child(child,persist=bool(self.db))
            except ValueError as exc:applog.warning("spawn_coordinator",f"Spawn blocked by safety ceiling: {exc}")
            except Exception as exc:applog.error("spawn_coordinator",f"Spawn failed closed: {exc}")
    def retire_child(self,policy_id:str,now:datetime|None=None)->bool:
        ex=self.executors.get(policy_id); now_ts=now or datetime.now(timezone.utc)
        if not ex or ex.status!="ACTIVE":return False
        ex.status="RETIRED"; events=[]
        for sym in list(ex.portfolio.positions):
            pos=ex.portfolio.positions[sym]; px=float(ex.portfolio.last_prices.get(sym,pos.entry_price)); trade=ex.portfolio.close_virtual_position(sym,px,"RETIRED_FLATTEN",now_ts)
            eid=hashlib.sha256(f"{policy_id}|{sym}|{now_ts.isoformat()}|RETIREMENT_EXIT".encode()).hexdigest()
            events.append({"id":eid,"policy_id":policy_id,"parent_policy_id":ex.config.parent_policy_id,"config_fingerprint":ex.config_fingerprint,
                "candidate_id":f"retire_{sym}_{int(now_ts.timestamp())}","ts":now_ts,"symbol":sym,"strategy":"retirement","direction":pos.side,
                "signal_generated":False,"hypothetical_final_decision":"REJECTED","disagreement_category":"NON_COMPARABLE","entry_price":pos.entry_price,
                "virtual_exit_price":trade["exit_price"],"virtual_exit_reason":"RETIRED_FLATTEN","realized_r":trade["realized_r"],"mfe_r":trade["mfe_r"],"mae_r":trade["mae_r"],"payload_obj":{"exit_record":trade}})
        if self.db:self.db.retire_spawned_strategy(policy_id); self.db.commit_spawn_observation(events,[(policy_id,ex.portfolio.to_checkpoint_dict())])
        self.children_retired+=1; return True
