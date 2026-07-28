"""Surgical remediation for 2026-07-28 heartbeat/runtime incidents.

Applies only runtime correctness changes. No strategy, ML/meta threshold, scanner score,
position sizing, stop distance, target economics, or market-risk policy changes.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rep(path, old, new, label):
    p = ROOT / path
    s = p.read_text(encoding="utf-8")
    if new in s:
        print("already", label); return
    if old not in s:
        raise RuntimeError(f"anchor missing: {label}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    print("applied", label)

# 1. Alpaca must return bracket/OCO children; otherwise Rule #41 sees TP but not stop.
rep("backend/app/brokers/alpaca.py",
    'GetOrdersRequest(status="open", limit=200),',
    'GetOrdersRequest(status="open", limit=200, nested=True),',
    "nested broker orders")

# 2. Event discovery is auxiliary; never await it from the production tick owner.
rep("backend/app/core/engine.py",
    '        self._last_event_monitor_min = None\n        self._last_intraday_scan_min: int | None = None',
    '        self._last_event_monitor_min = None\n        self._event_monitor_task: asyncio.Task | None = None\n        self._last_intraday_scan_min: int | None = None',
    "event task state")
rep("backend/app/core/engine.py",
    '                self._last_event_monitor_min = et_mins\n                await self.run_event_monitor(now)',
    '                self._last_event_monitor_min = et_mins\n                task = self._event_monitor_task\n                if task is None or task.done():\n                    if task is not None and not task.cancelled():\n                        try:\n                            task.result()\n                        except Exception as exc:\n                            applog.warning("engine", f"Event monitor failed open: {exc}")\n                    self._event_monitor_task = asyncio.create_task(self.run_event_monitor(now), name="event-monitor")',
    "nonblocking event monitor")
rep("backend/app/core/engine.py",
    '        # fix(#56): cancel emergency flatten task when engine is explicitly stopped',
    '        event_task = getattr(self, "_event_monitor_task", None)\n        if event_task is not None and not event_task.done():\n            event_task.cancel()\n            try:\n                await event_task\n            except asyncio.CancelledError:\n                pass\n        self._event_monitor_task = None\n        # fix(#56): cancel emergency flatten task when engine is explicitly stopped',
    "event task shutdown")

# 3. Reconciliation is observational and convergent. A single broker/local race is not
# proof of corruption. Two consecutive mismatches gate; verified convergence self-heals.
rep("backend/app/core/engine.py",
    '        self.reconciliation_failed = False\n        self.reconciliation_mismatch = False',
    '        self.reconciliation_failed = False\n        self.reconciliation_mismatch = False\n        self._position_reconciliation_mismatch_streak = 0\n        self._order_reconciliation_mismatch_streak = 0',
    "reconciliation state")

old = '''        # Monitor 8: Position reconciliation
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
new = '''        # Monitors 8/9: continuously re-evaluate broker truth. Alpaca can expose a
        # partial/flatten fill before the durable fill consumer has applied it locally.
        # Confirm mismatches across heartbeats and automatically clear reconciliation-only
        # SAFE_MODE once broker and durable lots converge.
        position_bad = False
        order_bad = False
        broker_syms: dict[str, float] = {}
        if diagnostics["broker_connected"]:
            try:
                broker_positions = await self.broker.positions()
                broker_syms = {p.symbol: float(p.qty) for p in broker_positions if hasattr(p, "symbol") and abs(float(p.qty)) > 1e-9}
                lots = getattr(self.portfolio, "_lots", {}) if self.portfolio else {}
                db_syms = {sym: float(lot.qty) for sym, lot in lots.items() if hasattr(lot, "qty") and abs(float(lot.qty)) > 1e-9} if isinstance(lots, dict) else {}
                position_bad = broker_syms != db_syms
                if position_bad:
                    diagnostics["positions_reconciled"] = False
                    self._position_reconciliation_mismatch_streak += 1
                    applog.warning("engine.heartbeat", f"Position reconciliation mismatch confirmation {self._position_reconciliation_mismatch_streak}/2. Broker: {broker_syms}, DB lots: {db_syms}")
                else:
                    self._position_reconciliation_mismatch_streak = 0
            except Exception as exc:
                diagnostics["positions_reconciled"] = False
                applog.debug("engine.heartbeat", f"Position reconciliation observation unavailable: {exc}")

            try:
                stale = []
                for cid, info in list(self.pending_reconciliation_orders.items()):
                    try:
                        order = await self.broker.get_order_by_client_id(cid)
                    except Exception as exc:
                        diagnostics["orders_reconciled"] = False
                        applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable for {cid}: {exc}")
                        continue
                    if order is None:
                        symbol = str((info or {}).get("symbol", "")).upper()
                        if symbol and symbol not in broker_syms:
                            stale.append(cid)
                        else:
                            order_bad = True
                for cid in stale:
                    self.pending_reconciliation_orders.pop(cid, None)
                if stale:
                    self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)
                    applog.info("engine.heartbeat", f"Cleared {len(stale)} terminal reconciliation order(s) for flat positions")
                if order_bad:
                    diagnostics["orders_reconciled"] = False
                    self._order_reconciliation_mismatch_streak += 1
                    applog.warning("engine.heartbeat", f"Order reconciliation mismatch confirmation {self._order_reconciliation_mismatch_streak}/2")
                else:
                    self._order_reconciliation_mismatch_streak = 0
            except Exception as exc:
                diagnostics["orders_reconciled"] = False
                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")

        confirmed_bad = self._position_reconciliation_mismatch_streak >= 2 or self._order_reconciliation_mismatch_streak >= 2
        if confirmed_bad:
            self.reconciliation_mismatch = True
            self.allow_new_entries = False
            if self.system_health != "HALTED":
                self.system_health = "SAFE_MODE"
        elif diagnostics["broker_connected"] and not position_bad and not order_bad:
            if self.reconciliation_mismatch:
                applog.info("engine.heartbeat", "Broker/local reconciliation restored; clearing reconciliation SAFE_MODE latch")
            self.reconciliation_mismatch = False
            diagnostics["positions_reconciled"] = True
            diagnostics["orders_reconciled"] = True
            if self.system_health == "SAFE_MODE":
                self.system_health = "NORMAL"
'''
rep("backend/app/core/engine.py", old, new, "convergent heartbeat reconciliation")

# 4. Telemetry must expose reconciliation failures; today UI omitted them from reasons.
rep("backend/app/core/engine.py",
    '        if not diag.get("buying_power_ok", True):\n            degraded_reasons.append("low_buying_power")\n        if not diag.get("vix_fresh", True):',
    '        if not diag.get("buying_power_ok", True):\n            degraded_reasons.append("low_buying_power")\n        if not diag.get("positions_reconciled", True):\n            degraded_reasons.append("positions_not_reconciled")\n        if not diag.get("orders_reconciled", True):\n            degraded_reasons.append("orders_not_reconciled")\n        if not diag.get("vix_fresh", True):',
    "reconciliation telemetry")

print("heartbeat/runtime root-cause patch applied")
