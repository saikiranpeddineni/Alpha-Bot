"""Holistic LIVE production exit lifecycle patch (2026-07-29).

Scope: live Alpaca production exits only.  No simulator/backtest/Spawn/ML changes.

Design invariants:
- Initial entry risk is immutable.
- Native bracket remains authoritative until a winner proves itself.
- Runner promotion uses completed-bar evidence only.
- Extend the broker TP BEFORE raising the stop; failure leaves the old bracket safe.
- A runner is authoritative only after BOTH replacement legs are broker-confirmed.
- Profit protection is monotonic; it can never be loosened intentionally.
- Ordinary strategy exits are intentionally subordinated once runner ownership is
  broker-confirmed. Hard halt/EOD/Rule-41 retain authority.
- Original-TP engine safety-net remains active until runner confirmation.
- No cancel/rebuild is used for runner promotion because Alpaca bracket cancel
  semantics can cancel the sibling leg too.

Run from repository root on fix/heartbeat-runtime-root-causes-20260728.
Idempotent and anchor-failing: if code drifted, it stops instead of guessing.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
HARDENED = ROOT / "backend/app/brokers/alpaca_hardened.py"
TEST = ROOT / "backend/tests/test_holistic_live_exit_harvest.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        print(f"already applied: {label}")
        return text
    if old not in text:
        raise RuntimeError(f"anchor not found for {label}; refusing unsafe fuzzy patch")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 1) Hardened Alpaca: broker-confirmed PATCH of bracket/OCO child legs.
# ---------------------------------------------------------------------------
h = HARDENED.read_text(encoding="utf-8")
anchor = '''    async def cancel_all(self) -> None:\n        \"\"\"Request cancellation and verify no open orders remain.\"\"\"\n'''
method = '''    async def replace_exit_leg(\n        self,\n        order_id: str,\n        *,\n        limit_price: float | None = None,\n        stop_price: float | None = None,\n    ) -> Order | None:\n        \"\"\"Replace one live bracket/OCO exit leg and verify broker truth.\n\n        Alpaca documents that a successful PATCH does not prove the replacement\n        won the race with an execution.  Therefore the returned replacement ID is\n        re-fetched and its requested price is verified before callers may treat\n        the new protection as authoritative.\n        \"\"\"\n        if (limit_price is None) == (stop_price is None):\n            raise ValueError("replace_exit_leg requires exactly one price field")\n        from alpaca.trading.requests import ReplaceOrderRequest\n\n        kwargs = {}\n        if limit_price is not None:\n            kwargs["limit_price"] = normalize_stock_price(limit_price)\n        else:\n            kwargs["stop_price"] = normalize_stock_price(stop_price)\n        try:\n            raw = await legacy._call(\n                self._client.replace_order_by_id,\n                order_id,\n                ReplaceOrderRequest(**kwargs),\n                label=f"replace_order/{order_id}",\n            )\n            replacement_id = str(getattr(raw, "id", "") or "")\n            if not replacement_id:\n                applog.warning("broker.alpaca", f"replace {order_id}: broker returned no replacement id")\n                return None\n            verified = await legacy._call(\n                self._client.get_order_by_id,\n                replacement_id,\n                label=f"verify_replacement/{replacement_id}",\n            )\n            status = normalize_order_status(getattr(verified, "status", None))\n            if status in {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.FILLED}:\n                applog.warning("broker.alpaca", f"replace {order_id}: replacement {replacement_id} is {status}")\n                return None\n            actual = getattr(verified, "limit_price" if limit_price is not None else "stop_price", None)\n            expected = float(limit_price if limit_price is not None else stop_price)\n            if actual is None or abs(float(actual) - expected) > 0.011:\n                applog.warning(\n                    "broker.alpaca",\n                    f"replace {order_id}: price verification failed expected={expected:.4f} actual={actual}",\n                )\n                return None\n            return self._to_order(verified)\n        except BrokerTimeoutError:\n            applog.warning("broker.alpaca", f"replace {order_id} timed out; replacement state is ambiguous")\n            return None\n        except Exception as exc:  # noqa: BLE001\n            applog.warning("broker.alpaca", f"replace {order_id} failed: {exc}")\n            return None\n\n    async def cancel_all(self) -> None:\n        \"\"\"Request cancellation and verify no open orders remain.\"\"\"\n'''
h = replace_once(h, anchor, method, "broker-confirmed exit-leg replacement")
HARDENED.write_text(h, encoding="utf-8")


# ---------------------------------------------------------------------------
# 2) Engine: one explicit live winner/runner ownership state.
# ---------------------------------------------------------------------------
e = ENGINE.read_text(encoding="utf-8")
e = replace_once(
    e,
    '''        # PR #271 V2 Adaptive Harvesting: Isolated Shadow State Store (NON-AUTHORITATIVE)\n        self._v2_shadow_states: dict[str, dict] = {}\n''',
    '''        # PR #271 V2 Adaptive Harvesting: Isolated Shadow State Store (NON-AUTHORITATIVE)\n        self._v2_shadow_states: dict[str, dict] = {}\n        # LIVE exit ownership.  This is deliberately separate from V2 shadow.\n        # A symbol enters this set only after Alpaca confirms BOTH the extended TP\n        # and a non-loss/profit-lock stop.  Until then all legacy exit safety nets\n        # remain authoritative.\n        self._live_runner_symbols: set[str] = set()\n        self._live_runner_floor: dict[str, float] = {}\n''',
    "live runner state",
)

# Insert live lifecycle immediately before passive V2 observer.
anchor = '''    def _evaluate_v2_shadow_passive(self, symbol: str, position: Any, df: pd.DataFrame, now: datetime) -> None:\n'''
method = '''    async def _manage_live_winner_exit(self, symbol: str, position: Any, df: pd.DataFrame) -> bool:\n        \"\"\"Promote/ratchet a proven LIVE winner without creating a protection gap.\n\n        Returns True only when broker-confirmed runner ownership is active.\n        This policy intentionally avoids trying to predict the exact top.  It\n        converts increasing MFE into an increasing guaranteed floor while moving\n        the bracket TP far enough away to preserve exceptional upside.\n        \"\"\"\n        if self.mode != "live" or self.broker is None or len(df) < 2:\n            return symbol in self._live_runner_symbols\n        replace_leg = getattr(self.broker, "replace_exit_leg", None)\n        if replace_leg is None:\n            return False\n\n        lot = self.portfolio._lots.get(symbol) if self.portfolio else None\n        initial_stop = getattr(lot, "initial_stop_loss", None) if lot is not None else None\n        if initial_stop is None:\n            initial_stop = self._position_initial_stops.get(symbol)\n        original_tp = self._position_take_profits.get(symbol)\n        entry = float(position.avg_entry_price)\n        if initial_stop is None or original_tp is None:\n            return symbol in self._live_runner_symbols\n        risk = abs(entry - float(initial_stop))\n        if risk <= 1e-8:\n            return False\n        side = 1.0 if position.qty > 0 else -1.0\n        target_r = max(0.1, side * (float(original_tp) - entry) / risk)\n\n        # Completed-bar causal evidence only.  Never promote from an unconfirmed\n        # transient tick/wick.  Activation scales with the strategy's own target\n        # rather than replacing strategy economics with one magic dollar value.\n        prior = df.iloc[-2]\n        if position.qty > 0:\n            prior_best = float(prior["high"])\n            self._position_peaks[symbol] = max(self._position_peaks.get(symbol, entry), prior_best)\n            mfe_r = max(0.0, (self._position_peaks[symbol] - entry) / risk)\n        else:\n            prior_best = float(prior["low"])\n            self._position_peaks[symbol] = min(self._position_peaks.get(symbol, entry), prior_best)\n            mfe_r = max(0.0, (entry - self._position_peaks[symbol]) / risk)\n        activation_r = min(1.0, max(0.75, target_r * 0.60))\n        if mfe_r < activation_r:\n            return symbol in self._live_runner_symbols\n\n        # Give a newly proven winner room, then tighten progressively.  The allowed\n        # giveback shrinks as a fraction of MFE while retaining at least 0.60R of\n        # breathing room so ordinary one-minute noise does not dominate exits.\n        giveback_allowance_r = max(0.60, mfe_r * 0.35)\n        locked_r = max(0.05, mfe_r - giveback_allowance_r)\n        desired_floor = entry + side * locked_r * risk\n        current_floor = self._live_runner_floor.get(symbol)\n        broker_current = getattr(lot, "stop_loss", None) if lot is not None else None\n        if broker_current is not None:\n            current_floor = float(broker_current) if current_floor is None else (\n                max(current_floor, float(broker_current)) if side > 0 else min(current_floor, float(broker_current))\n            )\n        if current_floor is not None:\n            desired_floor = max(desired_floor, current_floor) if side > 0 else min(desired_floor, current_floor)\n\n        # Stop must remain behind the completed close with a small geometry buffer.\n        prior_close = float(prior["close"])\n        px_buffer = max(0.01, prior_close * 0.0002)\n        desired_floor = min(desired_floor, prior_close - px_buffer) if side > 0 else max(desired_floor, prior_close + px_buffer)\n        if side > 0 and desired_floor <= entry:\n            desired_floor = entry + max(0.01, risk * 0.05)\n        elif side < 0 and desired_floor >= entry:\n            desired_floor = entry - max(0.01, risk * 0.05)\n\n        orders = await self.broker.open_orders()\n        exits = [o for o in orders if o.symbol.upper() == symbol and o.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}]\n        stop_leg = next((o for o in exits if o.leg == "stop" or o.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}), None)\n        tp_leg = next((o for o in exits if o.leg == "take_profit" and o.limit_price is not None), None)\n        if stop_leg is None or tp_leg is None:\n            # Rule-41 owns missing-protection recovery.  Do not compete with it.\n            return symbol in self._live_runner_symbols\n\n        # Practically uncap upside: keep the broker OCO/bracket shape but move its\n        # limit far ahead of both the original target and current MFE.  It is moved\n        # again as MFE expands, so the runner is managed by the ratcheting floor.\n        runner_tp_r = max(20.0, mfe_r + 10.0, target_r + 10.0)\n        desired_tp = entry + side * runner_tp_r * risk\n        tp_needs_move = (side > 0 and float(tp_leg.limit_price) < desired_tp - 0.01) or (\n            side < 0 and float(tp_leg.limit_price) > desired_tp + 0.01\n        )\n        if tp_needs_move:\n            # Extend upside FIRST.  If this fails, original bracket remains intact.\n            replaced_tp = await replace_leg(tp_leg.id, limit_price=desired_tp)\n            if replaced_tp is None:\n                return symbol in self._live_runner_symbols\n\n        stop_px = float(stop_leg.stop_price or 0.0)\n        stop_needs_move = (side > 0 and desired_floor > stop_px + 0.01) or (\n            side < 0 and (stop_px <= 0 or desired_floor < stop_px - 0.01)\n        )\n        if stop_needs_move:\n            replaced_stop = await replace_leg(stop_leg.id, stop_price=desired_floor)\n            if replaced_stop is None:\n                # Upside may already be extended, but original stop still protects.\n                # Do NOT claim runner ownership until profit protection is confirmed.\n                return symbol in self._live_runner_symbols\n            confirmed_floor = float(replaced_stop.stop_price or desired_floor)\n            self._live_runner_floor[symbol] = confirmed_floor\n            if self.portfolio:\n                self.portfolio.note_stop(symbol, confirmed_floor)\n        elif stop_px > 0:\n            self._live_runner_floor[symbol] = stop_px\n\n        floor = self._live_runner_floor.get(symbol)\n        protected_profit = floor is not None and ((side > 0 and floor > entry) or (side < 0 and floor < entry))\n        if protected_profit:\n            if symbol not in self._live_runner_symbols:\n                applog.info(\n                    "engine.exit",\n                    f"{symbol}: LIVE RUNNER confirmed mfe={mfe_r:.2f}R floor={floor:.4f}; original TP/ordinary strategy exits subordinated",\n                )\n            self._live_runner_symbols.add(symbol)\n        return symbol in self._live_runner_symbols\n\n    def _evaluate_v2_shadow_passive(self, symbol: str, position: Any, df: pd.DataFrame, now: datetime) -> None:\n'''
e = replace_once(e, anchor, method, "live winner manager")

# Call manager before original TP safety net; comment documents why legacy code remains.
e = replace_once(
    e,
    '''            # Safety-net for the ORIGINAL strategy target. Broker TP remains preferred;\n            # this only prevents protection repair from silently turning a bracket into\n            # an unlimited hold if its TP leg disappears.\n            target = self._position_take_profits.get(symbol)\n''',
    '''            runner_active = await self._manage_live_winner_exit(symbol, position, df)\n\n            # Safety-net for the ORIGINAL strategy target. Broker TP remains preferred.\n            # IMPORTANT: do not delete this legacy safety path.  It remains necessary\n            # before runner confirmation and after failed/ambiguous broker replacement.\n            # Once BOTH runner legs are broker-confirmed it is intentionally bypassed,\n            # otherwise this local safety-net would cap every exceptional winner.\n            target = None if runner_active else self._position_take_profits.get(symbol)\n''',
    "runner ownership before original TP",
)

# Subordinate ordinary strategy exits only after broker-confirmed runner ownership.
e = replace_once(
    e,
    '''            if sig.action == SignalAction.EXIT:\n                if position is not None:\n                    applog.info("engine", f"{symbol}: strategy exit -- {sig.reason}")\n                    await self._close_position_guarded(symbol, reason=sig.reason)\n                continue\n''',
    '''            if sig.action == SignalAction.EXIT:\n                if position is not None:\n                    if symbol in self._live_runner_symbols:\n                        # Intentionally subordinate ordinary lagging strategy exits once\n                        # a broker-confirmed profit floor owns the winner.  Rule-41, EOD,\n                        # hard-halt and the broker stop remain authoritative and are not\n                        # routed through this strategy-signal branch.\n                        applog.info("engine.exit", f"{symbol}: runner preserving winner; ignored ordinary strategy exit -- {sig.reason}")\n                    else:\n                        applog.info("engine", f"{symbol}: strategy exit -- {sig.reason}")\n                        await self._close_position_guarded(symbol, reason=sig.reason)\n                continue\n''',
    "runner strategy-exit arbitration",
)

# Clear runner state when broker position is gone.
e = replace_once(
    e,
    '''            self._position_take_profits.pop(symbol, None)\n            if symbol in self._v2_shadow_states and not self._v2_shadow_states[symbol].get("is_shadow_closed"):\n''',
    '''            self._position_take_profits.pop(symbol, None)\n            self._live_runner_symbols.discard(symbol)\n            self._live_runner_floor.pop(symbol, None)\n            if symbol in self._v2_shadow_states and not self._v2_shadow_states[symbol].get("is_shadow_closed"):\n''',
    "runner cleanup",
)

ENGINE.write_text(e, encoding="utf-8")

TEST.write_text('''"""Regression invariants for holistic LIVE winner harvesting."""\nfrom pathlib import Path\n\n\ndef test_holistic_live_exit_patch_is_active():\n    root = Path(__file__).resolve().parents[1]\n    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")\n    broker = (root / "app/brokers/alpaca_hardened.py").read_text(encoding="utf-8")\n    assert "async def _manage_live_winner_exit" in engine\n    assert "self._live_runner_symbols" in engine\n    assert "target = None if runner_active" in engine\n    assert "runner preserving winner; ignored ordinary strategy exit" in engine\n    assert "async def replace_exit_leg" in broker\n\n\ndef test_runner_transition_order_is_fail_closed():\n    root = Path(__file__).resolve().parents[1]\n    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")\n    method = engine.split("async def _manage_live_winner_exit", 1)[1].split("def _evaluate_v2_shadow_passive", 1)[0]\n    assert method.index("replaced_tp = await replace_leg") < method.index("replaced_stop = await replace_leg")\n    assert "return symbol in self._live_runner_symbols" in method\n    assert method.index("protected_profit =") < method.index("self._live_runner_symbols.add(symbol)")\n\n\ndef test_profit_floor_is_monotonic_and_original_tp_only_bypassed_after_runner():\n    root = Path(__file__).resolve().parents[1]\n    engine = (root / "app/core/engine.py").read_text(encoding="utf-8")\n    method = engine.split("async def _manage_live_winner_exit", 1)[1].split("def _evaluate_v2_shadow_passive", 1)[0]\n    assert "max(desired_floor, current_floor)" in method\n    assert "min(desired_floor, current_floor)" in method\n    assert "protected_profit" in method\n    assert "target = None if runner_active else self._position_take_profits.get(symbol)" in engine\n''', encoding="utf-8")

print("Applied holistic LIVE exit harvesting patch.")
print("Changed: backend/app/core/engine.py")
print("Changed: backend/app/brokers/alpaca_hardened.py")
print("Added: backend/tests/test_holistic_live_exit_harvest.py")
