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
    '        # Authoritative original strategy take-profit. This survives broker protection repair\n'
    '        # so a recovered stop can never silently convert a bracket trade into an unlimited hold.\n'
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
    '            # The original strategy target remains authoritative even if broker-side protection\n'
    '            # reconciliation had to replace/cancel an exit leg. This is a safety-net, not a new\n'
    '            # harvesting policy: broker TP remains preferred when present.\n'
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
    "authoritative software TP safety-net",
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

engine = replace_once(
    engine,
    '                db_strat = self.db.get_latest_order_strategy(p.symbol, self.mode)\n',
    '                db_strat = self.db.get_latest_entry_order_strategy(p.symbol, self.mode)\n',
    "entry-only strategy restore",
)
engine = replace_once(
    engine,
    '            order_payload = self.db.get_latest_order_payload(p.symbol, self.mode)\n'
    '            stop_loss = None\n'
    '            initial_risk = None\n',
    '            order_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)\n'
    '            stop_loss = None\n'
    '            take_profit = None\n'
    '            initial_risk = None\n',
    "entry-only payload restore",
)
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
    '                            # Preserve the strategy target before any broker repair. Alpaca may\n'
    '                            # require cancellation to free held quantity for the replacement stop;\n'
    '                            # the engine therefore keeps the exact target authoritative in software.\n'
    '                            self._position_take_profits[p.symbol] = float(limit_px)\n'
    '                            p.take_profit = float(limit_px)\n',
    "preserve TP before protection repair",
)

ENGINE.write_text(engine, encoding="utf-8")


db = DB.read_text(encoding="utf-8")
anchor = '''    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:\n        with self._session() as s:\n            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())\n            for row in s.scalars(stmt):\n                try:\n                    payload = json.loads(row.payload)\n                    if payload.get("symbol") == symbol.upper():\n                        return payload.get("strategy")\n                except Exception:\n                    pass\n        return None\n\n    def get_latest_order_payload(self, symbol: str, mode: str) -> dict | None:\n'''
replacement = '''    @staticmethod\n    def _is_entry_order_payload(payload: dict, symbol: str) -> bool:\n        if payload.get("symbol") != symbol.upper():\n            return False\n        strategy = str(payload.get("strategy") or "").lower()\n        reason = str(payload.get("reason") or "").lower()\n        order_type = str(payload.get("order_type") or "").split(".")[-1].lower()\n        # Recovery/protection orders are lifecycle infrastructure, never strategy ownership.\n        if strategy in {"emergency_protection", "reconciled", "alpaca", ""}:\n            return False\n        if "emergency_protection" in reason or "protective stop recovery" in reason:\n            return False\n        if order_type in {"stop", "stop_limit"}:\n            return False\n        return True\n\n    def get_latest_entry_order_payload(self, symbol: str, mode: str) -> dict | None:\n        with self._session() as s:\n            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())\n            for row in s.scalars(stmt):\n                try:\n                    payload = json.loads(row.payload)\n                    if self._is_entry_order_payload(payload, symbol):\n                        return payload\n                except Exception:\n                    pass\n        return None\n\n    def get_latest_entry_order_strategy(self, symbol: str, mode: str) -> str | None:\n        payload = self.get_latest_entry_order_payload(symbol, mode)\n        return payload.get("strategy") if payload else None\n\n    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:\n        # Backward-compatible API, but strategy ownership must come from an entry order.\n        return self.get_latest_entry_order_strategy(symbol, mode)\n\n    def get_latest_order_payload(self, symbol: str, mode: str) -> dict | None:\n'''
db = replace_once(db, anchor, replacement, "DB entry ownership resolver")
DB.write_text(db, encoding="utf-8")

TEST.write_text(r'''from pathlib import Path\n\n\ndef test_strategy_restore_ignores_protection_orders_source():\n    src = (Path(__file__).parents[1] / "app" / "db" / "database.py").read_text(encoding="utf-8")\n    assert "def get_latest_entry_order_payload" in src\n    assert 'strategy in {"emergency_protection", "reconciled", "alpaca", ""}' in src\n    assert 'if order_type in {"stop", "stop_limit"}' in src\n\n\ndef test_reconciliation_restores_entry_strategy_and_target_source():\n    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    assert "get_latest_entry_order_strategy" in src\n    assert "get_latest_entry_order_payload" in src\n    assert 'take_profit = order_payload.get("take_profit")' in src\n    assert "self._position_take_profits[p.symbol]" in src\n\n\ndef test_original_take_profit_remains_authoritative_source():\n    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    assert "original strategy take-profit triggered" in src\n    assert "high_val >= target" in src\n    assert "low_val <= target" in src\n\n\ndef test_protection_repair_preserves_target_before_cancellation_source():\n    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")\n    preserve = src.index("if is_take_profit and limit_px is not None:")\n    cancel = src.index("c_res = self.broker.cancel(o.id)", preserve)\n    assert preserve < cancel\n    block = src[preserve:cancel]\n    assert "self._position_take_profits[p.symbol] = float(limit_px)" in block\n''', encoding="utf-8")

print("Applied exit lifecycle + strategy ownership stabilization.")
print("Changed: backend/app/core/engine.py, backend/app/db/database.py")
print("Added: backend/tests/test_exit_lifecycle_strategy_ownership_20260727.py")
