"""Surgically harden the production entry/reconciliation boundary.

Fixes:
1. Reconcile ambiguous submissions before heartbeat consumes reconciliation state.
2. A single broker not-found observation never releases an idempotency/exposure guard.
3. Resolve broker absence only after a bounded age + repeated not-found evidence.
4. Heartbeat observes unresolved ambiguity but never independently deletes durable guards.
5. Reconciliation SAFE_MODE severity cannot be downgraded by a later DEGRADED monitor.

Run from repository root before the local test suite:
    python tools/apply_entry_exit_reconciliation_hardening_20260729.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
src = ENGINE.read_text(encoding="utf-8")
original = src

# Reconciliation must resolve durable submission ambiguity before heartbeat decides
# whether broker/local order state is healthy.
old = '''        # System Heartbeat Checks (CR-146 Phase F)\n        await self._check_system_heartbeat(now)\n        if self.state == EngineState.HALTED:\n            return\n\n        # Synchronize ML gate threshold with assessed market risk regime with dynamic scale'''
new = '''        # Resolve durable ambiguous submissions before heartbeat evaluates broker/local\n        # order reconciliation.  Heartbeat is an observer of this state, not a second\n        # owner that may independently delete idempotency guards.\n        try:\n            await self._reconcile_pending_submissions(now=now)\n        except Exception as exc:\n            applog.error("engine", f"Failed to reconcile pending submissions: {exc}")\n\n        # System Heartbeat Checks (CR-146 Phase F)\n        await self._check_system_heartbeat(now)\n        if self.state == EngineState.HALTED:\n            return\n\n        # Synchronize ML gate threshold with assessed market risk regime with dynamic scale'''
if old not in src:
    raise SystemExit("tick heartbeat anchor not found; refusing unsafe patch")
src = src.replace(old, new, 1)

old = '''        try:\n            await self._reconcile_pending_submissions()\n        except Exception as exc:\n            applog.error("engine", f"Failed to reconcile pending submissions: {exc}")\n\n        # Run pre-market scanner'''
if old not in src:
    raise SystemExit("old post-heartbeat reconciliation anchor not found")
src = src.replace(old, '''        # Run pre-market scanner''', 1)

start = src.index('    async def _reconcile_pending_submissions(')
end = src.index('    def _update_shadow_decision(', start)
replacement = '''    async def _reconcile_pending_submissions(self, now: datetime | None = None) -> None:\n        """Resolve ambiguous submits without releasing duplicate/exposure guards early.\n\n        Broker lookup returning ``None`` is negative evidence, not proof that an order\n        accepted around a transport failure can never appear.  Retain the durable guard\n        until absence is observed repeatedly and the submission has aged beyond a small\n        reconciliation window.  Positive broker truth resolves immediately.\n        """\n        if not self.pending_reconciliation_orders:\n            return\n\n        now = now or utcnow()\n        min_absence_age_seconds = 60.0\n        required_not_found_observations = 3\n        client_ids = list(self.pending_reconciliation_orders.keys())\n        applog.info("engine", f"Checking status of {len(client_ids)} ambiguous order submissions: {client_ids}")\n\n        changed = False\n        for cid in client_ids:\n            order_info = self.pending_reconciliation_orders.get(cid)\n            if not isinstance(order_info, dict):\n                continue\n            symbol = str(order_info.get("symbol", "")).upper()\n            try:\n                order = await self.broker.get_order_by_client_id(cid)\n            except Exception as exc:\n                # Query uncertainty can never be interpreted as broker absence.\n                applog.warning("engine", f"Reconciliation query failed for client ID {cid}: {exc}. Retaining guard.")\n                continue\n\n            if order is not None:\n                applog.info("engine", f"Ambiguous order resolved from broker truth: {symbol} ({cid}) status={order.status}.")\n                self.pending_reconciliation_orders.pop(cid, None)\n                self.db.save_order(order, self.mode)\n                changed = True\n                continue\n\n            submitted_at = order_info.get("submitted_at")\n            try:\n                submitted_dt = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))\n                if submitted_dt.tzinfo is None:\n                    submitted_dt = submitted_dt.replace(tzinfo=UTC)\n                age_seconds = max(0.0, (now - submitted_dt.astimezone(UTC)).total_seconds())\n            except (TypeError, ValueError):\n                # Missing/corrupt age evidence is not permission to release the guard.\n                age_seconds = 0.0\n\n            count = int(order_info.get("reconcile_not_found_count", 0) or 0) + 1\n            order_info["reconcile_not_found_count"] = count\n            order_info["last_reconcile_not_found_at"] = now.isoformat()\n            if not order_info.get("first_reconcile_not_found_at"):\n                order_info["first_reconcile_not_found_at"] = now.isoformat()\n            changed = True\n\n            if count < required_not_found_observations or age_seconds < min_absence_age_seconds:\n                applog.warning(\n                    "engine",\n                    f"Ambiguous order still unresolved: {symbol} ({cid}) broker_not_found "\n                    f"observation={count}/{required_not_found_observations} age={age_seconds:.1f}s; retaining guard.",\n                )\n                continue\n\n            applog.warning(\n                "engine",\n                f"Ambiguous order resolved absent after bounded reconciliation: {symbol} ({cid}) "\n                f"not_found_observations={count} age={age_seconds:.1f}s. Treating as REJECTED.",\n            )\n            self.pending_reconciliation_orders.pop(cid, None)\n            rejected_order = Order(\n                id=cid,\n                client_order_id=cid,\n                symbol=symbol,\n                side=OrderSide.BUY if str(order_info.get("side", "")).lower() == "buy" else OrderSide.SELL,\n                qty=float(order_info.get("qty", 0.0) or 0.0),\n                order_type=OrderType.LIMIT if str(order_info.get("order_type", "")).lower() == "limit" else OrderType.MARKET,\n                limit_price=order_info.get("limit_price"),\n                status=OrderStatus.REJECTED,\n                reason=(\n                    "Ambiguous submit resolved absent after repeated broker not-found "\n                    f"observations ({count}) and {age_seconds:.1f}s age."\n                ),\n            )\n            self.db.save_order(rejected_order, self.mode)\n\n        if changed:\n            self.db.set_setting("pending_reconciliation_orders", self.pending_reconciliation_orders)\n\n'''
src = src[:start] + replacement + src[end:]

# Heartbeat must not be a competing cleanup owner.  It should simply mark durable
# unresolved ambiguity as unreconciled so entry gating remains fail-closed.
old_start = src.index('            try:\n                stale = []\n                for cid, info in list(self.pending_reconciliation_orders.items()):')
old_end = src.index('            except Exception as exc:\n                diagnostics["orders_reconciled"] = False\n                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")', old_start)
old_end += len('            except Exception as exc:\n                diagnostics["orders_reconciled"] = False\n                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")')
heartbeat_replacement = '''            try:\n                # Durable ambiguous submissions are owned by\n                # _reconcile_pending_submissions().  Heartbeat never deletes them.\n                # While any remain, broker/local order truth is unresolved and new\n                # exposure must remain fail-closed.\n                order_bad = bool(self.pending_reconciliation_orders)\n                if order_bad:\n                    diagnostics["orders_reconciled"] = False\n                    self._order_reconciliation_mismatch_streak += 1\n                    applog.warning(\n                        "engine.heartbeat",\n                        f"Order reconciliation unresolved confirmation "\n                        f"{self._order_reconciliation_mismatch_streak}/2: "\n                        f"pending_client_ids={list(self.pending_reconciliation_orders.keys())}",\n                    )\n                else:\n                    self._order_reconciliation_mismatch_streak = 0\n            except Exception as exc:\n                diagnostics["orders_reconciled"] = False\n                order_bad = True\n                applog.debug("engine.heartbeat", f"Order reconciliation observation unavailable: {exc}")'''
src = src[:old_start] + heartbeat_replacement + src[old_end:]

# Enforce a final severity floor before SafetyDecision construction.  Individual
# monitors may report DEGRADED, but they may not downgrade a reconciliation latch.
anchor = '''        # Phase F2: Market Risk Evaluation\n        market_risk_state, risk_multiplier, allowed_strategies, risk_allow_entries = self._evaluate_market_risk(now)'''
replacement = '''        # Health severity is monotonic within a heartbeat.  A later, less-severe\n        # monitor (for example broker connectivity -> DEGRADED) must never overwrite\n        # an already-latched reconciliation SAFE_MODE.\n        if self.state == EngineState.HALTED or getattr(self.risk, "hard_halted", False):\n            self.system_health = "HALTED"\n            self.allow_new_entries = False\n        elif getattr(self, "reconciliation_mismatch", False):\n            self.system_health = "SAFE_MODE"\n            self.allow_new_entries = False\n\n        # Phase F2: Market Risk Evaluation\n        market_risk_state, risk_multiplier, allowed_strategies, risk_allow_entries = self._evaluate_market_risk(now)'''
if anchor not in src:
    raise SystemExit("health severity anchor not found")
src = src.replace(anchor, replacement, 1)

if src == original:
    raise SystemExit("no changes applied")
ENGINE.write_text(src, encoding="utf-8")
print("Applied entry/reconciliation hardening to backend/app/core/engine.py")
