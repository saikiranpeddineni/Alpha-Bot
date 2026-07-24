"""Live Out-of-Band Shadow Spawn Coordinator.

Manages active spawned child strategy variants during live/paper engine execution.
Evaluates market observations out-of-band via a non-blocking queue and persists
durable research events and portfolio checkpoints to SQLite in atomic transactions.
Zero connection to production Broker execution methods.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

from app.core.logging import applog
from app.db.database import Database
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor


class SpawnShadowCoordinator:
    """Out-of-band live runtime coordinator for spawned challenger policies."""

    def __init__(self, db: Database | None = None, max_queue_size: int = 1000) -> None:
        self.db = db
        self.max_queue_size = max_queue_size
        self.executors: dict[str, ChildPolicyExecutor] = {}
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self.worker_task: asyncio.Task | None = None
        self.running: bool = False
        self.shadow_events_dropped: int = 0

    def telemetry(self) -> dict[str, Any]:
        """Returns telemetry metrics for queue drops and active executors."""
        return {
            "shadow_events_dropped": self.shadow_events_dropped,
            "queue_size": self.queue.qsize(),
            "executors_count": len(self.executors),
            "running": self.running,
        }

    def initialize_from_db(self) -> int:
        """Loads all ACTIVE spawned strategies from database, validates fingerprints, and restores checkpoints."""
        if not self.db:
            return 0
        try:
            active_rows = self.db.get_active_spawned_strategies()
            count = 0
            for row in active_rows:
                policy_id = row["policy_id"]
                cfg_json = row.get("config_snapshot_json")
                expected_fp = row.get("config_fingerprint")
                
                if not cfg_json:
                    applog.warning("spawn_coordinator", f"Policy {policy_id} missing config snapshot. Marking UNAVAILABLE.")
                    config = ChildPolicyConfig(
                        policy_id=policy_id,
                        seed_capital=float(row.get("seed_capital", 10000.0)),
                        status="UNAVAILABLE",
                        unavailable_reason="MISSING_CONFIG_SNAPSHOT"
                    )
                else:
                    try:
                        cfg_dict = json.loads(cfg_json)
                        config = ChildPolicyConfig.from_dict(cfg_dict)
                        # Validate stored fingerprint matches computed fingerprint
                        computed_fp = config.compute_fingerprint()
                        if expected_fp and expected_fp != computed_fp:
                            applog.error(
                                "spawn_coordinator",
                                f"Policy {policy_id} fingerprint mismatch! Expected {expected_fp[:8]}, computed {computed_fp[:8]}. Marking UNAVAILABLE."
                            )
                            config.status = "UNAVAILABLE"
                            config.unavailable_reason = "FINGERPRINT_MISMATCH"
                    except Exception as e:
                        applog.error("spawn_coordinator", f"Failed to parse config snapshot for {policy_id}: {e}. Marking UNAVAILABLE.")
                        config = ChildPolicyConfig(
                            policy_id=policy_id,
                            seed_capital=float(row.get("seed_capital", 10000.0)),
                            status="UNAVAILABLE",
                            unavailable_reason=f"CORRUPT_CONFIG_SNAPSHOT: {e}"
                        )

                executor = ChildPolicyExecutor(config)
                # Restore stateful portfolio checkpoint if active and present
                if executor.status == "ACTIVE":
                    chk = self.db.get_spawn_portfolio_checkpoint(policy_id)
                    if chk:
                        executor.portfolio.restore_from_checkpoint(chk)

                self.executors[policy_id] = executor
                if executor.status == "ACTIVE":
                    count += 1
            applog.info("spawn_coordinator", f"Initialized {count} active child policy executors from DB.")
            return count
        except Exception as e:
            applog.error("spawn_coordinator", f"Error initializing spawned strategies: {e}")
            return 0

    async def start(self) -> None:
        """Starts the background worker task to process queued market observations."""
        if self.running:
            return
        self.running = True
        self.initialize_from_db()
        self.worker_task = asyncio.create_task(self._background_worker())
        applog.info("spawn_coordinator", "SpawnShadowCoordinator background worker started.")

    async def stop(self) -> None:
        """Gracefully stops the background worker task."""
        if not self.running:
            return
        self.running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        applog.info("spawn_coordinator", "SpawnShadowCoordinator background worker stopped.")

    def publish_observation(self, observation: dict[str, Any]) -> bool:
        """Non-blocking publish of a market observation snapshot to the queue.
        Fail-open for Champion trading: if queue is full or coordinator stopped, return False silently.
        """
        if not self.running or not self.executors:
            return False
        try:
            self.queue.put_nowait(observation)
            return True
        except asyncio.QueueFull:
            self.shadow_events_dropped += 1
            applog.warning(
                "spawn_coordinator",
                f"Shadow observation queue full (dropped={self.shadow_events_dropped}). Snapshot dropped."
            )
            return False
        except Exception as e:
            applog.warning("spawn_coordinator", f"Failed to publish shadow observation: {e}")
            return False

    async def _background_worker(self) -> None:
        """Background worker loop processing queue items asynchronously out-of-band."""
        while self.running:
            try:
                obs = await self.queue.get()
                await self._process_observation_out_of_band(obs)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                applog.error("spawn_coordinator", f"Error in shadow background worker: {e}")

    async def _process_observation_out_of_band(self, obs: dict[str, Any]) -> None:
        """Offloads evaluation AND atomic database persistence off the main asyncio event loop."""
        def _evaluate_and_persist_sync():
            events = []
            checkpoints = []
            for policy_id, executor in list(self.executors.items()):
                try:
                    evt = executor.evaluate_observation(obs)
                    if evt:
                        events.append(evt)
                        checkpoints.append((policy_id, executor.portfolio.to_checkpoint_dict()))
                except Exception as ex:
                    applog.error("spawn_coordinator", f"Error evaluating child policy {policy_id}: {ex}")
            
            if self.db and (events or checkpoints):
                try:
                    self.db.commit_spawn_observation(events, checkpoints)
                except Exception as db_err:
                    applog.error("spawn_coordinator", f"Atomic DB commit failed for shadow observation: {db_err}")

        try:
            await asyncio.to_thread(_evaluate_and_persist_sync)
        except Exception as e:
            applog.error("spawn_coordinator", f"Error in offloaded evaluation/persistence thread: {e}")

    def retire_child(self, policy_id: str, now: datetime | None = None) -> bool:
        """Retires an active child strategy, flattens virtual positions, and saves final checkpoint."""
        executor = self.executors.get(policy_id)
        now_ts = now or datetime.now(timezone.utc)
        if executor:
            executor.status = "RETIRED"
            # Flatten open virtual positions deterministically
            open_symbols = list(executor.portfolio.positions.keys())
            for sym in open_symbols:
                pos = executor.portfolio.positions.get(sym)
                if pos:
                    executor.portfolio.close_virtual_position(sym, pos.entry_price, "RETIRED_FLATTEN", now_ts)
            
            if self.db:
                self.db.retire_spawned_strategy(policy_id)
                self.db.commit_spawn_observation([], [(policy_id, executor.portfolio.to_checkpoint_dict())])
            applog.info("spawn_coordinator", f"Retired child policy {policy_id} and flattened virtual positions.")
            return True
        return False
