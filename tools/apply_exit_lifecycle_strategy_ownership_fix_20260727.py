from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
DB = ROOT / "backend/app/db/database.py"
TEST = ROOT / "backend/tests/test_exit_lifecycle_strategy_ownership_20260727.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            print(f"already applied: {label}")
            return text
        raise RuntimeError(f"anchor not found for {label}")
    return text.replace(old, new, 1)


engine = ENGINE.read_text(encoding="utf-8")
engine = replace_once(
    engine,
    '        self._position_initial_stops: dict[str, float | None] = {}\n',
    '        self._position_initial_stops: dict[str, float | None] = {}\n'
    '        # Original strategy target survives broker protection repair/reconciliation.\n'
    '        self._position_take_profits: dict[str, float] = {}\n',
    "take-profit state",
)
engine = replace_once(
    engine,
    '            # PR #271 V2 Adaptive Harvesting: PASSIVE OBSERVER (NON-AUTHORITATIVE)\n'
    '            self._evaluate_v2_shadow_passive(symbol, position, df, now)\n'
    '            \n'
    '            if position.qty > 0:\n',
    '            # PR #271 V2 Adaptive Harvesting: PASSIVE OBSERVER (NON-AUTHORITATIVE)\n'
    '            self._evaluate_v2_shadow_passive(symbol, position, df, now)\n'
    '\n'
    '            # Safety-net for the ORIGINAL strategy target. Broker TP remains preferred;\n'
    '            # this only prevents protection repair from silently turning a bracket into\n'
    '            # an unlimited hold if its TP leg disappears.\n'
    '            target = self._position_take_profits.get(symbol)\n'
    '            if target is None:\n'
    '                broker_target = getattr(position, "take_profit", None)\n'
    '                if broker_target is not None and float(broker_target) > 0:\n'
    '                    target = float(broker_target)\n'
    '                    self._position_take_profits[symbol] = target\n'
    '            if target is not None:\n'
    '                target_hit = (position.qty > 0 and high_val >= target) or (position.qty < 0 and low_val <= target)\n'
    '                if target_hit:\n'
    '                    applog.info("engine", f"{symbol}: original strategy take-profit triggered @ {target:.4f}")\n'
    '                    await self._close_position_guarded(symbol, reason=f"original strategy take-profit triggered ({target:.4f})")\n'
    '                    self._position_peaks.pop(symbol, None)\n'
    '                    self._position_troughs.pop(symbol, None)\n'
    '                    self._position_initial_stops.pop(symbol, None)\n'
    '                    self._position_take_profits.pop(symbol, None)\n'
    '                    return []\n'
    '\n'
    '            if position.qty > 0:\n',
    "software TP safety-net",
)
engine = replace_once(
    engine,
    '            if symbol in self._position_initial_stops:\n'
    '                del self._position_initial_stops[symbol]\n'
    '            if symbol in self._v2_shadow_states and not self._v2_shadow_states[symbol].get("is_shadow_closed"):\n',
    '            if symbol in self._position_initial_stops:\n'
    '                del self._position_initial_stops[symbol]\n'
    '            self._position_take_profits.pop(symbol, None)\n'
    '            if symbol in self._v2_shadow_states and not self._v2_shadow_states[symbol].get("is_shadow_closed"):\n',
    "TP cleanup",
)
# Ownership patch may already be present locally. The existing get_latest_order_strategy API
# is entry-only after the DB patch, so either form is safe.
if 'db_strat = self.db.get_latest_entry_order_strategy(p.symbol, self.mode)' not in engine:
    engine = replace_once(
        engine,
        '                db_strat = self.db.get_latest_order_strategy(p.symbol, self.mode)\n',
        '                db_strat = self.db.get_latest_entry_order_strategy(p.symbol, self.mode)\n',
        "entry-only strategy restore",
    )

# Handle both pristine and already-applied ownership reconciliation blocks.
old_pristine = ('            order_payload = self.db.get_latest_order_payload(p.symbol, self.mode)\n'
                '            stop_loss = None\n'
                '            initial_risk = None\n')
old_owned = ('            entry_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)\n'
             '            order_payload = entry_payload or self.db.get_latest_order_payload(p.symbol, self.mode)\n'
             '            if entry_payload and entry_payload.get("strategy"):\n'
             '                strat = str(entry_payload["strategy"])\n'
             '                p.strategy = strat\n'
             '                if hasattr(self.broker, "_symbol_strategies"):\n'
             '                    self.broker._symbol_strategies[p.symbol] = strat\n'
             '            stop_loss = None\n'
             '            initial_risk = None\n')
new_owned = ('            entry_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)\n'
             '            order_payload = entry_payload or self.db.get_latest_order_payload(p.symbol, self.mode)\n'
             '            if entry_payload and entry_payload.get("strategy"):\n'
             '                strat = str(entry_payload["strategy"])\n'
             '                p.strategy = strat\n'
             '                if hasattr(self.broker, "_symbol_strategies"):\n'
             '                    self.broker._symbol_strategies[p.symbol] = strat\n'
             '            stop_loss = None\n'
             '            take_profit = None\n'
             '            initial_risk = None\n')
if new_owned not in engine:
    if old_owned in engine:
        engine = engine.replace(old_owned, new_owned, 1)
    elif old_pristine in engine:
        engine = engine.replace(old_pristine, new_owned, 1)
    else:
        raise RuntimeError("anchor not found for ownership-compatible payload restore")

engine = replace_once(
    engine,
    '                stop_loss = order_payload.get("stop_loss")\n'
    '                if stop_loss:\n'
    '                    initial_risk = abs(p.avg_entry_price - stop_loss)\n',
    '                stop_loss = order_payload.get("stop_loss")\n'
    '                take_profit = order_payload.get("take_profit")\n'
    '                if stop_loss:\n'
    '                    initial_risk = abs(p.avg_entry_price - stop_loss)\n'
    '                if take_profit is not None and float(take_profit) > 0:\n'
    '                    self._position_take_profits[p.symbol] = float(take_profit)\n'
    '                    p.take_profit = float(take_profit)\n',
    "restore original TP",
)
engine = replace_once(
    engine,
    '                        is_bracket_or_oco_parent = (\n'
    '                            order_class in {"bracket", "oco", "oto"}\n'
    '                            and not is_stop_order\n'
    '                            and not is_take_profit\n'
    '                        )\n',
    '                        is_bracket_or_oco_parent = (\n'
    '                            order_class in {"bracket", "oco", "oto"}\n'
    '                            and not is_stop_order\n'
    '                            and not is_take_profit\n'
    '                        )\n'
    '\n'
    '                        if is_take_profit and limit_px is not None:\n'
    '                            # Preserve target before any repair cancellation.\n'
    '                            self._position_take_profits[p.symbol] = float(limit_px)\n'
    '                            p.take_profit = float(limit_px)\n',
    "preserve TP before protection repair",
)
ENGINE.write_text(engine, encoding="utf-8")

# Upgrade DB ownership resolver only if the earlier ownership patch has not already done so.
db = DB.read_text(encoding="utf-8")
if 'def get_latest_entry_order_strategy(' not in db:
    if 'def get_latest_entry_order_payload(' in db:
        marker = '    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:\n'
        addition = ('    def get_latest_entry_order_strategy(self, symbol: str, mode: str) -> str | None:\n'
                    '        payload = self.get_latest_entry_order_payload(symbol, mode)\n'
                    '        return payload.get("strategy") if payload else None\n\n')
        if marker not in db:
            raise RuntimeError("DB strategy marker missing")
        db = db.replace(marker, addition + marker, 1)
    else:
        raise RuntimeError("DB ownership patch missing: run apply_position_strategy_ownership_fix_20260727.py first")
DB.write_text(db, encoding="utf-8")

TEST.write_text('''from pathlib import Path\n\n\ndef test_strategy_restore_uses_entry_ownership_source():\n    db = (Path(__file__).parents[1] / "app" / "db" / "database.py").read_text(encoding="utf-8")\n    engine = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    assert "def get_latest_entry_order_payload" in db\n    assert "def get_latest_entry_order_strategy" in db\n    assert 'non_owning_strategies = {"emergency_protection"}' in db\n    assert "get_latest_entry_order_payload(p.symbol, self.mode)" in engine\n\n\ndef test_reconciliation_restores_original_target_source():\n    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    assert 'take_profit = order_payload.get("take_profit")' in src\n    assert "self._position_take_profits[p.symbol] = float(take_profit)" in src\n\n\ndef test_original_take_profit_has_live_safety_net_source():\n    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    assert "original strategy take-profit triggered" in src\n    assert "high_val >= target" in src\n    assert "low_val <= target" in src\n\n\ndef test_protection_repair_preserves_target_before_cancel_source():\n    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    preserve = src.index("if is_take_profit and limit_px is not None:")\n    cancel = src.index("c_res = self.broker.cancel(o.id)", preserve)\n    assert preserve < cancel\n    assert "self._position_take_profits[p.symbol] = float(limit_px)" in src[preserve:cancel]\n''', encoding="utf-8")

print("Applied exit lifecycle + strategy ownership stabilization.")
print("Changed: backend/app/core/engine.py, backend/app/db/database.py")
print("Added: backend/tests/test_exit_lifecycle_strategy_ownership_20260727.py")
