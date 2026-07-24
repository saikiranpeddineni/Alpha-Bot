"""Out-of-band coordinator for the bounded SHADOW-ONLY spawn laboratory."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from app.core.logging import applog
from app.db.database import Database
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor


class SpawnShadowCoordinator:
    """Runs research children out-of-band. This class has no broker or promotion authority."""
    def __init__(self, db: Database | None = None, max_queue_size: int = 1000,
                 evolution_settings: EvolutionSettings | None = None, evolution_interval: int = 250) -> None:
        self.db = db; self.max_queue_size = max_queue_size
        self.executors: dict[str, ChildPolicyExecutor] = {}
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self.worker_task: asyncio.Task | None = None; self.running = False
        self.shadow_events_dropped = 0; self.observations_processed = 0
        self.evolution_interval = max(1, int(evolution_interval))
        self.evolution = SpawnEvolutionEngine(evolution_settings)
        self.children_spawned = 0; self.children_retired = 0; self.evolution_cycles = 0

    def telemetry(self) -> dict[str, Any]:
        active = sum(e.status == "ACTIVE" for e in self.executors.values())
        return {"shadow_events_dropped": self.shadow_events_dropped, "queue_size": self.queue.qsize(),
                "executors_count": len(self.executors), "active_children": active, "running": self.running,
                "observations_processed": self.observations_processed, "evolution_cycles": self.evolution_cycles,
                "children_spawned": self.children_spawned, "children_retired": self.children_retired,
                "generation": self.evolution.generation}

    def initialize_from_db(self) -> int:
        if not self.db: return 0
        try:
            count = 0
            for row in self.db.get_active_spawned_strategies():
                policy_id=row["policy_id"]; cfg_json=row.get("config_snapshot_json"); expected_fp=row.get("config_fingerprint")
                if not cfg_json:
                    config=ChildPolicyConfig(policy_id=policy_id, seed_capital=float(row.get("seed_capital",10000)), status="UNAVAILABLE", unavailable_reason="MISSING_CONFIG_SNAPSHOT")
                else:
                    try:
                        config=ChildPolicyConfig.from_dict(json.loads(cfg_json))
                        if expected_fp and expected_fp != config.compute_fingerprint():
                            config.status="UNAVAILABLE"; config.unavailable_reason="FINGERPRINT_MISMATCH"
                            applog.error("spawn_coordinator", f"Policy {policy_id} fingerprint mismatch; disabled.")
                    except Exception as exc:
                        config=ChildPolicyConfig(policy_id=policy_id, seed_capital=float(row.get("seed_capital",10000)), status="UNAVAILABLE", unavailable_reason=f"CORRUPT_CONFIG_SNAPSHOT:{exc}")
                executor=ChildPolicyExecutor(config)
                if executor.status == "ACTIVE":
                    chk=self.db.get_spawn_portfolio_checkpoint(policy_id)
                    if chk:
                        try: executor.portfolio.restore_from_checkpoint(chk)
                        except Exception as exc:
                            executor.status="UNAVAILABLE"; executor.unavailable_reason=f"CORRUPT_CHECKPOINT:{exc}"
                            applog.error("spawn_coordinator", f"Policy {policy_id} checkpoint rejected: {exc}")
                self.executors[policy_id]=executor
                if executor.status == "ACTIVE": count += 1
            applog.info("spawn_coordinator", f"Initialized {count} active shadow children.")
            return count
        except Exception as exc:
            applog.error("spawn_coordinator", f"Initialization failed closed for research layer: {exc}"); return 0

    async def start(self) -> None:
        if self.running:return
        self.running=True; self.initialize_from_db(); self.worker_task=asyncio.create_task(self._background_worker())
        applog.info("spawn_coordinator","Shadow spawn worker started.")

    async def stop(self) -> None:
        if not self.running:return
        self.running=False
        if self.worker_task:
            self.worker_task.cancel()
            try: await self.worker_task
            except asyncio.CancelledError: pass
        applog.info("spawn_coordinator","Shadow spawn worker stopped.")

    def publish_observation(self, observation: dict[str, Any]) -> bool:
        if not self.running or not self.executors:return False
        try:self.queue.put_nowait(observation); return True
        except asyncio.QueueFull:
            self.shadow_events_dropped+=1; return False
        except Exception as exc:
            applog.warning("spawn_coordinator",f"Shadow publish failed: {exc}"); return False

    async def _background_worker(self) -> None:
        while self.running:
            try:
                obs=await self.queue.get()
                try: await self._process_observation_out_of_band(obs)
                finally: self.queue.task_done()
            except asyncio.CancelledError:break
            except Exception as exc:applog.error("spawn_coordinator",f"Shadow worker error: {exc}")

    async def _process_observation_out_of_band(self, obs: dict[str, Any]) -> None:
        def work():
            events=[]; checkpoints=[]
            for policy_id,executor in list(self.executors.items()):
                try:
                    evt=executor.evaluate_observation(obs)
                    if evt:
                        events.append(evt); checkpoints.append((policy_id,executor.portfolio.to_checkpoint_dict()))
                except Exception as exc:applog.error("spawn_coordinator",f"Child {policy_id} evaluation failed: {exc}")
            if self.db and (events or checkpoints):
                try:self.db.commit_spawn_observation(events,checkpoints)
                except Exception as exc:applog.error("spawn_coordinator",f"Shadow atomic persistence failed: {exc}")
        await asyncio.to_thread(work)
        self.observations_processed += 1
        if self.observations_processed % self.evolution_interval == 0:
            await asyncio.to_thread(self._run_evolution_cycle_sync, obs.get("ts"))

    def add_child(self, config: ChildPolicyConfig, persist: bool = True) -> bool:
        """Register a new immutable shadow child. Duplicate identities are rejected."""
        if config.execution_mode != "SHADOW_ONLY" or config.policy_id in self.executors:return False
        fp=config.compute_fingerprint()
        if any(e.config_fingerprint == fp for e in self.executors.values()):return False
        executor=ChildPolicyExecutor(config)
        if executor.status != "ACTIVE":return False
        if persist and self.db:
            self.db.save_spawned_strategy(policy_id=config.policy_id,seed_capital=config.seed_capital,
                parent_policy_id=config.parent_policy_id,policy_version=config.policy_version,
                config_snapshot_json=config.snapshot_json(),config_fingerprint=fp,
                created_from_candidate=config.created_from_candidate,spawn_reason=config.spawn_reason,
                execution_mode="SHADOW_ONLY",code_commit_sha=config.code_commit_sha,
                model_manifest_sha=config.model_manifest_sha,spawned_at=config.spawned_at)
        self.executors[config.policy_id]=executor; self.children_spawned+=1; return True

    def _run_evolution_cycle_sync(self, now: datetime | None = None) -> None:
        """Bounded select/retire/reproduce cycle. Failures affect research only."""
        if not isinstance(now,datetime) or now.tzinfo is None: now=datetime.now(timezone.utc)
        self.evolution_cycles += 1
        for policy_id in self.evolution.retirement_candidates(self.executors):
            try:self.retire_child(policy_id,now)
            except Exception as exc:applog.error("spawn_coordinator",f"Retirement failed for {policy_id}: {exc}")
        for child in self.evolution.next_generation(self.executors,now):
            try:self.add_child(child,persist=True)
            except ValueError as exc:applog.warning("spawn_coordinator",f"Spawn skipped by safety ceiling: {exc}")
            except Exception as exc:applog.error("spawn_coordinator",f"Spawn failed closed: {exc}")

    def retire_child(self, policy_id: str, now: datetime | None = None) -> bool:
        executor=self.executors.get(policy_id); now_ts=now or datetime.now(timezone.utc)
        if not executor or executor.status != "ACTIVE":return False
        executor.status="RETIRED"; retire_events=[]
        for sym in list(executor.portfolio.positions):
            pos=executor.portfolio.positions[sym]
            # Research retirement must use the last causally observed mark, never fabricate entry-price flat P&L.
            exit_px=float(executor.portfolio.last_prices.get(sym,pos.entry_price))
            trade=executor.portfolio.close_virtual_position(sym,exit_px,"RETIRED_FLATTEN",now_ts)
            evt_id=hashlib.sha256(f"{policy_id}|{sym}|{now_ts.isoformat()}|RETIREMENT_EXIT".encode()).hexdigest()
            retire_events.append({"id":evt_id,"policy_id":policy_id,"parent_policy_id":executor.config.parent_policy_id,
                "config_fingerprint":executor.config_fingerprint,"candidate_id":f"retire_{sym}_{int(now_ts.timestamp())}","ts":now_ts,
                "symbol":sym,"strategy":"retirement","direction":pos.side,"signal_generated":False,
                "hypothetical_final_decision":"REJECTED","disagreement_category":"NON_COMPARABLE","entry_price":pos.entry_price,
                "virtual_exit_price":trade["exit_price"],"virtual_exit_reason":"RETIRED_FLATTEN","realized_r":trade["realized_r"],
                "mfe_r":trade["mfe_r"],"mae_r":trade["mae_r"],"payload_obj":{"exit_record":trade}})
        if self.db:
            self.db.retire_spawned_strategy(policy_id)
            self.db.commit_spawn_observation(retire_events,[(policy_id,executor.portfolio.to_checkpoint_dict())])
        self.children_retired+=1; return True
