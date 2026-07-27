from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/app/db/database.py"
ENGINE = ROOT / "backend/app/core/engine.py"
TEST = ROOT / "backend/tests/test_position_strategy_ownership_20260727.py"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"Expected patch anchor not found in {path}: {old[:100]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


old_db = '''    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:\n        with self._session() as s:\n            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())\n            for row in s.scalars(stmt):\n                try:\n                    payload = json.loads(row.payload)\n                    if payload.get("symbol") == symbol.upper():\n                        return payload.get("strategy")\n                except Exception:\n                    pass\n        return None\n\n    def get_latest_order_payload(self, symbol: str, mode: str) -> dict | None:\n'''
new_db = '''    def get_latest_entry_order_payload(self, symbol: str, mode: str) -> dict | None:\n        \"\"\"Return the newest true entry order for an active position.\n\n        Protective/recovery orders are operational children of an existing position;\n        they must never become the position's owning strategy on restart.\n        \"\"\"\n        symbol = symbol.upper()\n        non_owning_strategies = {"emergency_protection"}\n        with self._session() as s:\n            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())\n            for row in s.scalars(stmt):\n                try:\n                    payload = json.loads(row.payload)\n                    if str(payload.get("symbol", "")).upper() != symbol:\n                        continue\n                    strategy = str(payload.get("strategy") or "").strip()\n                    leg = str(payload.get("leg") or "entry").lower()\n                    if strategy in non_owning_strategies or leg != "entry":\n                        continue\n                    return payload\n                except Exception:\n                    pass\n        return None\n\n    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:\n        payload = self.get_latest_entry_order_payload(symbol, mode)\n        return payload.get("strategy") if payload else None\n\n    def get_latest_order_payload(self, symbol: str, mode: str) -> dict | None:\n'''
replace_once(DB, old_db, new_db)

old_engine = '''            order_payload = self.db.get_latest_order_payload(p.symbol, self.mode)\n            stop_loss = None\n'''
new_engine = '''            # Position ownership comes from the latest true ENTRY, never from a\n            # newer emergency/protective order.  This keeps UI attribution and\n            # strategy-specific exit management bound to the strategy that opened\n            # the surviving position.\n            entry_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)\n            order_payload = entry_payload or self.db.get_latest_order_payload(p.symbol, self.mode)\n            if entry_payload and entry_payload.get("strategy"):\n                strat = str(entry_payload["strategy"])\n                p.strategy = strat\n                if hasattr(self.broker, "_symbol_strategies"):\n                    self.broker._symbol_strategies[p.symbol] = strat\n            stop_loss = None\n'''
replace_once(ENGINE, old_engine, new_engine)

TEST.write_text('''from datetime import UTC, datetime, timedelta\n\nfrom app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position\nfrom app.db.database import Database\n\n\ndef _order(symbol, strategy, *, minutes, leg="entry", side=OrderSide.BUY):\n    return Order(\n        id=f"{symbol}-{strategy}-{minutes}",\n        symbol=symbol,\n        side=side,\n        qty=10,\n        order_type=OrderType.MARKET,\n        status=OrderStatus.FILLED,\n        filled_qty=10,\n        strategy=strategy,\n        leg=leg,\n        submitted_at=datetime.now(UTC) + timedelta(minutes=minutes),\n    )\n\n\ndef test_latest_strategy_ignores_newer_emergency_protection(tmp_path):\n    db = Database(tmp_path / "strategy.db")\n    db.save_order(_order("SQQQ", "orb", minutes=0), "paper")\n    db.save_order(_order("SQQQ", "emergency_protection", minutes=5, leg="stop", side=OrderSide.SELL), "paper")\n\n    assert db.get_latest_order_strategy("SQQQ", "paper") == "orb"\n    assert db.get_latest_entry_order_payload("SQQQ", "paper")["strategy"] == "orb"\n\n\ndef test_latest_true_entry_wins_when_multiple_entries_exist(tmp_path):\n    db = Database(tmp_path / "strategy.db")\n    db.save_order(_order("PLTR", "rsi2_reversion", minutes=0), "paper")\n    db.save_order(_order("PLTR", "orb", minutes=10), "paper")\n    db.save_order(_order("PLTR", "emergency_protection", minutes=15, leg="stop", side=OrderSide.SELL), "paper")\n\n    assert db.get_latest_order_strategy("PLTR", "paper") == "orb"\n\n\ndef test_position_strategy_field_is_mutable_for_reconciliation():\n    p = Position(symbol="NOK", qty=-5, avg_entry_price=9.23, market_price=9.01, strategy="emergency_protection")\n    p.strategy = "momentum"\n    assert p.strategy == "momentum"\n''', encoding="utf-8")

print("Applied immutable entry-strategy ownership fix and regression tests.")
