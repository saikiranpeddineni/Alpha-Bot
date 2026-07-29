"""Apply the 2026-07-28 runtime-safety root-cause fixes.

This is intentionally surgical and idempotent. It changes only:
- backend/app/core/engine.py
- backend/app/brokers/alpaca.py

Root causes addressed:
1. Heartbeat reconciliation was a permanent latch: once reconciliation_mismatch
   became true the heartbeat stopped re-checking reconciliation, so SAFE_MODE
   could never converge naturally.
2. A single transient broker/local snapshot race could latch SAFE_MODE.
3. Event monitoring ran inline in the trading tick and had no whole-run deadline.
4. Alpaca open-order retrieval did not request nested bracket/OCO legs, so valid
   broker-side protective children could be invisible to Rule-41 verification.

The recovery rule deliberately never writes system_health=NORMAL. Clearing a
reconciliation latch only removes that one cause; the next heartbeat recomputes
health from every independent monitor, preventing reconciliation recovery from
masking DB, broker, scanner, buying-power, model, or hard-halt conditions.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
ALPACA = ROOT / "backend/app/brokers/alpaca.py"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    src = path.read_text(encoding="utf-8")
    if new in src:
        print(f"already applied: {label}")
        return
    if old not in src:
        raise RuntimeError(f"anchor not found for {label}: {path}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")


# ---------------------------------------------------------------------------
# Alpaca: expose bracket/OCO child legs to authoritative protection checks.
# ---------------------------------------------------------------------------
replace_once(
    ALPACA,
    'GetOrdersRequest(status="open", limit=200),',
    'GetOrdersRequest(status="open", limit=200, nested=True),',
    "Alpaca nested open orders",
)


# ---------------------------------------------------------------------------
# Engine state: explicit task ownership + reconciliation confirmation streaks.
# ---------------------------------------------------------------------------
replace_once(
    ENGINE,
    "        self._last_event_monitor_min = None\n        self._last_intraday_scan_min: int | None = None",
    "        self._last_event_monitor_min = None\n"
    "        self._event_monitor_task: asyncio.Task | None = None\n"
    "        self._reconciliation_bad_streak = 0\n"
    "        self._reconciliation_good_streak = 0\n"
    "        self._last_intraday_scan_min: int | None = None",
    "event-monitor task and reconciliation streak state",
)

# Explicitly cancel the monitor on engine stop. A detached task must not outlive
# the engine/broker/data objects it reads.
replace_once(
    ENGINE,
    "        # fix(#56): cancel emergency flatten task when engine is explicitly stopped\n"
    "        if self._emergency_flatten_task is not None and not self._emergency_flatten_task.done():",
    "        if self._event_monitor_task is not None and not self._event_monitor_task.done():\n"
    "            self._event_monitor_task.cancel()\n"
    "            try:\n"
    "                await self._event_monitor_task\n"
    "            except asyncio.CancelledError:\n"
    "                pass\n"
    "            self._event_monitor_task = None\n"
    "        # fix(#56): cancel emergency flatten task when engine is explicitly stopped\n"
    "        if self._emergency_flatten_task is not None and not self._emergency_flatten_task.done():",
    "event-monitor lifecycle cancellation",
)

# Move pending-submission reconciliation ahead of heartbeat order checks. This
# removes the false SAFE_MODE window where an ambiguous submit could be resolved
# later in the same tick but heartbeat had already latched the discrepancy.
replace_once(
    ENGINE,
    "        # System Heartbeat Checks (CR-146 Phase F)\n"
    "        await self._check_system_heartbeat(now)\n"
    "        if self.state == EngineState.HALTED:\n"
    "            return\n\n"
    "        # Synchronize ML gate threshold",
    "        # Resolve ambiguous submissions before heartbeat reconciliation so the\n"
    "        # heartbeat evaluates the freshest authoritative order state.\n"
    "        try:\n"
    "            await self._reconcile_pending_submissions()\n"
    "        except Exception as exc:\n"
    "            self.reconciliation_failed = True\n"
    "            applog.error(\"engine\", f\"Failed to reconcile pending submissions: {exc}\")\n\n"
    "        # System Heartbeat Checks (CR-146 Phase F)\n"
    "        await self._check_system_heartbeat(now)\n"
    "        if self.state == EngineState.HALTED:\n"
    "            return\n\n"
    "        # Synchronize ML gate threshold",
    "reconcile ambiguous submissions before heartbeat",
)

# Remove the now-duplicate later reconciliation call.
replace_once(
    ENGINE,
    "        try:\n"
    "            await self._reconcile_pending_submissions()\n"
    "        except Exception as exc:\n"
    "            applog.error(\"engine\", f\"Failed to reconcile pending submissions: {exc}\")\n\n"
    "        # Run pre-market scanner",
    "        # Run pre-market scanner",
    "remove duplicate pending-submission reconciliation",
)

# Event monitor: never await the wide-universe monitor inline in the trading
# tick. One task at a time, bounded by a whole-run timeout, with failure isolated.
replace_once(
    ENGINE,
    "            if 9 * 60 + 30 <= et_mins <= 16 * 60 and et_mins % 5 == 0 and self._last_event_monitor_min != et_mins:\n"
    "                self._last_event_monitor_min = et_mins\n"
    "                await self.run_event_monitor(now)",
    "            if 9 * 60 + 30 <= et_mins <= 16 * 60 and et_mins % 5 == 0 and self._last_event_monitor_min != et_mins:\n"
    "                self._last_event_monitor_min = et_mins\n"
    "                if self._event_monitor_task is None or self._event_monitor_task.done():\n"
    "                    self._event_monitor_task = asyncio.create_task(\n"
    "                        self._run_event_monitor_bounded(now),\n"
    "                        name=\"trading-engine-event-monitor\",\n"
    "                    )\n"
    "                else:\n"
    "                    applog.warning(\"engine\", \"Event monitor still running; skipping overlapping invocation\")",
    "non-blocking event-monitor scheduling",
)

# Add a whole-run deadline immediately before run_event_monitor(). The timeout
# owns liveness at the orchestration boundary even if a future provider/calendar
# implementation forgets to bound one of its I/O calls.
replace_once(
    ENGINE,
    "    async def run_event_monitor(self, now: datetime) -> None:\n",
    "    async def _run_event_monitor_bounded(self, now: datetime) -> None:\n"
    "        try:\n"
    "            await asyncio.wait_for(self.run_event_monitor(now), timeout=45.0)\n"
    "        except asyncio.TimeoutError:\n"
    "            applog.error(\"engine\", \"Event monitor exceeded 45s deadline; cancelled without blocking trading tick\")\n"
    "        except asyncio.CancelledError:\n"
    "            raise\n"
    "        except Exception as exc:\n"
    "            applog.error(\"engine\", f\"Event monitor background failure: {exc}\")\n\n"
    "    async def run_event_monitor(self, now: datetime) -> None:\n",
    "bounded event-monitor runner",
)

# Per-symbol deadline prevents gather() from waiting on one provider call until
# the whole-run timeout. It also releases semaphore capacity predictably.
replace_once(
    ENGINE,
    '                    df = await self.data.history(symbol, "1Min", start_time, now)',
    '                    df = await asyncio.wait_for(\n'
    '                        self.data.history(symbol, "1Min", start_time, now),\n'
    '                        timeout=12.0,\n'
    '                    )',
    "per-symbol event-monitor I/O deadline",
)

# ---------------------------------------------------------------------------
# Heartbeat reconciliation: replace permanent latch with convergent confirmation.
# ---------------------------------------------------------------------------
old_position_block = '''        # Monitor 8: Position reconciliation
        if diagnostics["broker_connected"] and not getattr(self, "reconciliation_mismatch", False):
            try:
                broker_positions = await self.broker.positions()
                broker_syms = {p.symbol: float(p.qty) for p in broker_positions if hasattr(p, "symbol")}
                lots = getattr(self.portfolio, "_lots", {}) if self.portfolio else {}
                if isinstance(lots, dict):
                    db_syms = {sym: float(lot.qty) for sym, lot in lots.items() if hasattr(lot, "qty") and lot.qty != 0}
                    reconciled = True
                    for sym, qty in broker_syms.items():
                        if sym not in db_syms or db_syms[sym] != qty:
                            reconciled = False
                            break
                    if reconciled:
                        for sym, qty in db_syms.items():
                            if sym not in broker_syms or broker_syms[sym] != qty:
                                reconciled = False
                                break
                                
                    if not reconciled:
                        diagnostics["positions_reconciled"] = False
                        self.reconciliation_mismatch = True
                        self.allow_new_entries = False
                        if self.system_health not in ("HALTED",):
                            self.system_health = "SAFE_MODE"
                        applog.warning("engine.heartbeat", f"Position reconciliation failure. Broker: {broker_syms}, DB lots: {db_syms}")
            except Exception as exc:
                applog.debug("engine.heartbeat", f"Position reconciliation check skipped/failed: {exc}")
                self.reconciliation_mismatch = True
                self.allow_new_entries = False
                if self.system_health not in ("HALTED",):
                    self.system_health = "SAFE_MODE"

        # Monitor 9: Order reconciliation
        if diagnostics["broker_connected"] and not getattr(self, "reconciliation_mismatch", False):
            try:
                discrepancy = False
                for cid in list(self.pending_reconciliation_orders.keys()):
                    try:
                        order = await self.broker.get_order_by_client_id(cid)
                        if order is None:
                            discrepancy = True
                            break
                    except Exception:
                        discrepancy = True
                        break
                        
                if discrepancy:
                    diagnostics["orders_reconciled"] = False
                    self.reconciliation_mismatch = True
                    self.allow_new_entries = False
                    if self.system_health not in ("HALTED",):
                        self.system_health = "SAFE_MODE"
                    applog.warning("engine.heartbeat", "Order reconciliation mismatch found.")
            except Exception:
                diagnostics["orders_reconciled"] = False
                self.reconciliation_mismatch = True
                self.allow_new_entries = False
                if self.system_health not in ("HALTED",):
                    self.system_health = "SAFE_MODE"
'''

new_position_block = '''        # Monitors 8/9: authoritative reconciliation with convergence.
        # A single broker/local race is not enough to latch SAFE_MODE; two consecutive
        # mismatching snapshots are required. Once latched, two consecutive clean
        # snapshots clear only the reconciliation cause. We intentionally DO NOT set
        # system_health=NORMAL here: every independent monitor keeps its own authority.
        reconciliation_snapshot_ok = diagnostics["broker_connected"]
        if reconciliation_snapshot_ok:
            try:
                broker_positions = await self.broker.positions()
                broker_syms = {p.symbol: float(p.qty) for p in broker_positions if hasattr(p, "symbol")}
                lots = getattr(self.portfolio, "_lots", {}) if self.portfolio else {}
                db_syms = {
                    sym: float(lot.qty)
                    for sym, lot in lots.items()
                    if hasattr(lot, "qty") and abs(float(lot.qty)) > 1e-9
                } if isinstance(lots, dict) else {}
                positions_ok = broker_syms == db_syms
                diagnostics["positions_reconciled"] = positions_ok

                orders_ok = True
                for cid in list(self.pending_reconciliation_orders.keys()):
                    order = await self.broker.get_order_by_client_id(cid)
                    if order is None:
                        orders_ok = False
                        break
                diagnostics["orders_reconciled"] = orders_ok
                reconciliation_snapshot_ok = positions_ok and orders_ok

                if not positions_ok:
                    applog.warning(
                        "engine.heartbeat",
                        f"Position reconciliation snapshot mismatch. Broker: {broker_syms}, DB lots: {db_syms}",
                    )
                if not orders_ok:
                    applog.warning("engine.heartbeat", "Order reconciliation snapshot mismatch found.")
            except Exception as exc:
                reconciliation_snapshot_ok = False
                diagnostics["positions_reconciled"] = False
                diagnostics["orders_reconciled"] = False
                self.reconciliation_failed = True
                applog.warning("engine.heartbeat", f"Authoritative reconciliation snapshot failed: {exc}")

        if reconciliation_snapshot_ok:
            self._reconciliation_bad_streak = 0
            self._reconciliation_good_streak += 1
            if self.reconciliation_mismatch and self._reconciliation_good_streak >= 2:
                self.reconciliation_mismatch = False
                self.reconciliation_failed = False
                applog.info(
                    "engine.heartbeat",
                    "Reconciliation converged after two clean authoritative snapshots; SAFE_MODE cause cleared.",
                )
        else:
            self._reconciliation_good_streak = 0
            self._reconciliation_bad_streak += 1
            self.allow_new_entries = False
            if self._reconciliation_bad_streak >= 2 or self.reconciliation_mismatch:
                self.reconciliation_mismatch = True
                if self.system_health != "HALTED":
                    self.system_health = "SAFE_MODE"
            else:
                # First mismatch is fail-closed for entries but does not permanently
                # latch SAFE_MODE; this absorbs the known fill/local-state race.
                if self.system_health not in ("HALTED", "SAFE_MODE"):
                    self.system_health = "DEGRADED"
'''
replace_once(
    ENGINE,
    old_position_block,
    new_position_block,
    "convergent reconciliation state machine",
)

# Compile both modified production modules before reporting success.
for path in (ENGINE, ALPACA):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")

print("2026-07-28 root-cause runtime safety fixes applied; syntax compile passed.")
