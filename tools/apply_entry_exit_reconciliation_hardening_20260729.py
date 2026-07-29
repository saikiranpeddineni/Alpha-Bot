"""Surgically harden the production entry/reconciliation boundary.

Idempotent applicator: already-materialized sections are accepted; genuine code drift
still fails closed instead of applying fuzzy edits.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

old = '''        # System Heartbeat Checks (CR-146 Phase F)\n        await self._check_system_heartbeat(now)\n        if self.state == EngineState.HALTED:\n            return\n\n        # Synchronize ML gate threshold with assessed market risk regime with dynamic scale'''
new = '''        # Resolve durable ambiguous submissions before heartbeat evaluates broker/local\n        # order reconciliation.  Heartbeat is an observer of this state, not a second\n        # owner that may independently delete idempotency guards.\n        try:\n            await self._reconcile_pending_submissions(now=now)\n        except Exception as exc:\n            applog.error("engine", f"Failed to reconcile pending submissions: {exc}")\n\n        # System Heartbeat Checks (CR-146 Phase F)\n        await self._check_system_heartbeat(now)\n        if self.state == EngineState.HALTED:\n            return\n\n        # Synchronize ML gate threshold with assessed market risk regime with dynamic scale'''
if new not in src:
    if old not in src:
        raise SystemExit("tick heartbeat anchor not found; refusing unsafe patch")
    src = src.replace(old, new, 1)

old_post = '''        try:\n            await self._reconcile_pending_submissions()\n        except Exception as exc:\n            applog.error("engine", f"Failed to reconcile pending submissions: {exc}")\n\n        # Run pre-market scanner'''
if old_post in src:
    src = src.replace(old_post, '''        # Run pre-market scanner''', 1)
elif 'await self._reconcile_pending_submissions(now=now)' not in src:
    raise SystemExit("pending-submission ordering state is neither old nor hardened")

start = src.index('    async def _reconcile_pending_submissions(')
end = src.index('    def _update_shadow_decision(', start)
replacement = '''    async def _reconcile_pending_submissions(self, now: datetime | None = None) -> None:\n        """Resolve ambiguous submits without releasing duplicate/exposure guards early."""\n        if not self.pending_reconciliation_orders:\n            return\n        now = now or utcnow()\n        min_absence_age_seconds = 60.0\n        required_not_found_observations = 3\n        client_ids = list(self.pending_reconciliation_orders.keys())\n        applog.info("engine", f"Checking status of {len(client_ids)} ambiguous order submissions: {client_ids}")\n        changed = False\n        for cid in client_ids:\n            order_info = self.pending_reconciliation_orders.get(cid)\n            if not isinstance(order_info, dict):\n                continue\n            symbol = str(order_info.get("symbol", "")).upper()\n            try:\n                order = await self.broker.get_order_by_client_id(cid)\n            except Exception as exc:\n                applog.warning("engine", f"Reconciliation query failed for client ID {cid}: {exc}. Retaining guard.")\n                continue\n            if order is not None:\n                applog.info("engine", f"Ambiguous order resolved from broker truth: {symbol} ({cid}) status={order.status}.")\n                self.pending_reconciliation_orders.pop(cid, None)\n                self.db.save_order(order, self.mode)\n                changed = True\n                continue\n            submitted_at = order_info.get("submitted_at")\n            try:\n                submitted_dt = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))\n                if submitted_dt.tzinfo is None:\n                    submitted_dt = submitted_dt.replace(tzinfo=UTC)\n                age_seconds = max(0.0, (now - submitted_dt.astimezone(UTC)).total_seconds())\n            except (TypeError, ValueError):\n                age_seconds = 0.0\n            count = int(order_info.get("reconcile_not_found_count", 0) or 0) + 1\n            order_info["reconcile_not_found_count"] = count\n            order_info["last_reconcile_not_found_at"] = now.isoformat()\n            if not order_info.get("first_reconcile_not_found_at"):\n                order_info["first_reconcile_not_found_at"] = now.isoformat()\n            changed = True\n            if count < required_not_found_observations or age_seconds < min_absence_age_seconds:\n                applog.warning("engine", f"Ambiguous order still unresolved: {symbol} ({cid}) broker_not_found observation={count}/{required_not_found_observations} age={age_seconds:.1f}s; retaining guard.")\n                continue\n            applog.warning("engine", f"Ambiguous order resolved absent after bounded reconciliation: {symbol} ({cid}) not_found_observations={count} age={age_seconds:.1f}s. Treating as REJECTED.")\n            self.pending_reconciliation_orders.pop(cid, None)\n            rejected_order = Order(id=cid, client_order_id=cid, symbol=symbol, side=OrderSide.BUY if str(order_info.get("side", "")).lower() == "buy" else OrderSide.SELL, qty=float(order_info.get("qty", 0.0) or 0.0), order_type=OrderType.LIMIT if str(order_info.get("order_type", "")).lower() == "limit" else OrderType.MARKET, limit_price=order_info.get("limit_price"), status=OrderStatus.REJECTED, reason=f"Ambiguous submit resolved absent after repeated broker not-found observations ({count}) and {age_seconds:.1f}s age.")\n            self.db.save_order(rejected_order, self.mode)\n        if changed:\n            self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)\n\n'''
# Replace method only if its hardened bounded-reconciliation contract is absent.
current_method = src[start:end]
if 'required_not_found_observations = 3' not in current_method or 'min_absence_age_seconds = 60.0' not in current_method:
    src = src[:start] + replacement + src[end:]

legacy_marker = '                stale = []\n                for cid, info in list(self.pending_reconciliation_orders.items()):'
heartbeat_marker = 'Durable ambiguous submissions are owned by'
if legacy_marker in src:
    old_start = src.index('            try:\n                stale = []\n                for cid, info in list(self.pending_reconciliation_orders.items()):')
    old_end = src.index('            except Exception as exc:\n                diagnostics["orders_reconciled"] = False\n                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")', old_start)
    old_end += len('            except Exception as exc:\n                diagnostics["orders_reconciled"] = False\n                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")')
    heartbeat_replacement = '''            try:\n                # Durable ambiguous submissions are owned by\n                # _reconcile_pending_submissions(). Heartbeat never deletes them.\n                order_bad = bool(self.pending_reconciliation_orders)\n                if order_bad:\n                    diagnostics["orders_reconciled"] = False\n                    self._order_reconciliation_mismatch_streak += 1\n                    applog.warning("engine.heartbeat", f"Order reconciliation unresolved confirmation {self._order_reconciliation_mismatch_streak}/2: pending_client_ids={list(self.pending_reconciliation_orders.keys())}")\n                else:\n                    self._order_reconciliation_mismatch_streak = 0\n            except Exception as exc:\n                diagnostics["orders_reconciled"] = False\n                order_bad = True\n                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")'''
    src = src[:old_start] + heartbeat_replacement + src[old_end:]
elif heartbeat_marker not in src:
    raise SystemExit("heartbeat reconciliation ownership anchor not found")

anchor = '''        # Phase F2: Market Risk Evaluation\n        market_risk_state, risk_multiplier, allowed_strategies, risk_allow_entries = self._evaluate_market_risk(now)'''
severity = '''        # Health severity is monotonic within a heartbeat.  A later, less-severe\n        # monitor (for example broker connectivity -> DEGRADED) must never overwrite\n        # an already-latched reconciliation SAFE_MODE.\n        if self.state == EngineState.HALTED or getattr(self.risk, "hard_halted", False):\n            self.system_health = "HALTED"\n            self.allow_new_entries = False\n        elif getattr(self, "reconciliation_mismatch", False):\n            self.system_health = "SAFE_MODE"\n            self.allow_new_entries = False\n\n'''
if severity not in src:
    if anchor not in src:
        raise SystemExit("health severity anchor not found")
    src = src.replace(anchor, severity + anchor, 1)

if src != original:
    ENGINE.write_text(src, encoding="utf-8")
    print("Applied entry/reconciliation hardening to backend/app/core/engine.py")
else:
    print("Entry/reconciliation hardening already applied")
