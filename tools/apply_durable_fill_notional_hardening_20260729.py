"""Persist cumulative fill notional with the durable quantity watermark.

Repeat-safe applicator. Existing hardened DB/tracker/broker state is accepted; missing
legacy anchors only fail when the corresponding hardened invariant is also absent.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/app/db/database.py"
TRACKER = ROOT / "backend/app/portfolio/tracker.py"
ALPACA = ROOT / "backend/app/brokers/alpaca.py"

src = DB.read_text(encoding="utf-8")
old_model = '''class FillProgressRow(Base):\n    __tablename__ = "broker_fill_progress"\n    mode: Mapped[str] = mapped_column(String(20), primary_key=True)\n    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)\n    fill_key: Mapped[str] = mapped_column(String(96), primary_key=True)\n    cumulative_qty: Mapped[float] = mapped_column(Float)\n    processed_at: Mapped[datetime]\n'''
new_model = old_model.replace('    processed_at: Mapped[datetime]\n', '    cumulative_notional: Mapped[float] = mapped_column(Float, default=0.0)\n    processed_at: Mapped[datetime]\n')
if new_model not in src:
    if old_model not in src:
        raise SystemExit("fill-progress schema state unexpected")
    src = src.replace(old_model, new_model, 1)

migration_anchor = '''                if needs_nominee_commit:\n                    conn.commit()\n'''
if 'PRAGMA table_info(broker_fill_progress)' not in src:
    if migration_anchor not in src:
        raise SystemExit("fill-progress migration anchor missing")
    src = src.replace(migration_anchor, migration_anchor + '''\n                # Backward-compatible migration for durable partial-fill pricing.\n                cursor = conn.exec_driver_sql("PRAGMA table_info(broker_fill_progress)")\n                fill_progress_cols = {row[1] for row in cursor.fetchall()}\n                if "cumulative_notional" not in fill_progress_cols:\n                    conn.exec_driver_sql(\n                        "ALTER TABLE broker_fill_progress ADD COLUMN cumulative_notional FLOAT DEFAULT 0.0"\n                    )\n                    conn.commit()\n''', 1)

old_qty = '''    def processed_fill_qty(self, mode: str, account_id: str, fill_key: str) -> float:\n        with self._session() as s:\n            row = s.get(FillProgressRow, (mode, account_id, fill_key))\n            return float(row.cumulative_qty) if row else 0.0\n'''
new_qty = old_qty + '''\n    def processed_fill_notional(self, mode: str, account_id: str, fill_key: str) -> float:\n        with self._session() as s:\n            row = s.get(FillProgressRow, (mode, account_id, fill_key))\n            return float(row.cumulative_notional or 0.0) if row else 0.0\n'''
if 'def processed_fill_notional' not in src:
    if old_qty not in src:
        raise SystemExit("processed-fill accessor anchor missing")
    src = src.replace(old_qty, new_qty, 1)

old_sig = '''        self, *, fill_key: str, cumulative_qty: float, mode: str, account_id: str,\n        checkpoint: dict, trade: TradeRecord | None,\n'''
new_sig = '''        self, *, fill_key: str, cumulative_qty: float, cumulative_notional: float = 0.0, mode: str, account_id: str,\n        checkpoint: dict, trade: TradeRecord | None,\n'''
if new_sig not in src:
    if old_sig not in src:
        raise SystemExit("fill commit signature state unexpected")
    src = src.replace(old_sig, new_sig, 1)

old_merge = '''            s.merge(FillProgressRow(mode=mode, account_id=account_id, fill_key=fill_key,\n                                    cumulative_qty=cumulative_qty, processed_at=datetime.now(UTC)))\n'''
new_merge = '''            s.merge(FillProgressRow(mode=mode, account_id=account_id, fill_key=fill_key,\n                                    cumulative_qty=cumulative_qty, cumulative_notional=cumulative_notional,\n                                    processed_at=datetime.now(UTC)))\n'''
if new_merge not in src:
    if old_merge not in src:
        raise SystemExit("fill progress merge state unexpected")
    src = src.replace(old_merge, new_merge, 1)
DB.write_text(src, encoding="utf-8")

src = TRACKER.read_text(encoding="utf-8")
old_tracker = '''                processed = db.processed_fill_qty(mode=fill.broker_mode, account_id=fill.broker_account_id or "default_account", fill_key=fill.broker_fill_key)\n                delta = float(fill.cumulative_qty) - processed\n'''
new_tracker = '''                account_id = fill.broker_account_id or "default_account"\n                processed = db.processed_fill_qty(mode=fill.broker_mode, account_id=account_id, fill_key=fill.broker_fill_key)\n                previous_notional = db.processed_fill_notional(mode=fill.broker_mode, account_id=account_id, fill_key=fill.broker_fill_key)\n                delta = float(fill.cumulative_qty) - processed\n'''
if new_tracker not in src:
    if old_tracker not in src:
        raise SystemExit("tracker watermark state unexpected")
    src = src.replace(old_tracker, new_tracker, 1)
old_commit = '''                    db.commit_fill_processing(fill_key=fill.broker_fill_key, cumulative_qty=float(fill.cumulative_qty), mode=fill.broker_mode, account_id=fill.broker_account_id or "default_account", checkpoint=self.checkpoint(risk_state=risk_state), trade=trade)\n'''
new_commit = '''                    cumulative_notional = previous_notional + float(effective.qty) * float(effective.price)\n                    db.commit_fill_processing(fill_key=fill.broker_fill_key, cumulative_qty=float(fill.cumulative_qty), cumulative_notional=cumulative_notional, mode=fill.broker_mode, account_id=account_id, checkpoint=self.checkpoint(risk_state=risk_state), trade=trade)\n'''
if new_commit not in src:
    if old_commit not in src:
        raise SystemExit("tracker fill commit state unexpected")
    src = src.replace(old_commit, new_commit, 1)
TRACKER.write_text(src, encoding="utf-8")

src = ALPACA.read_text(encoding="utf-8")
old_broker = '''            else:\n                # A durable DB watermark can survive a process restart while the\n                # volatile notional cannot. Use the broker cumulative average as\n                # the conservative price basis; quantity de-dup remains exact.\n                prev_notional = processed * cum_avg_price\n'''
new_broker = '''            else:\n                # A durable DB watermark survives restart; use its matching cumulative\n                # notional so the next delta price remains exact as well as its quantity.\n                durable_notional = 0.0\n                if db:\n                    durable_notional = db.processed_fill_notional(\n                        mode=self._broker_mode, account_id=self.account_id, fill_key=oid\n                    )\n                prev_notional = durable_notional if durable_notional > 0 else processed * cum_avg_price\n'''
if new_broker not in src:
    if old_broker not in src:
        raise SystemExit("Alpaca restart notional state unexpected")
    src = src.replace(old_broker, new_broker, 1)
ALPACA.write_text(src, encoding="utf-8")
print("Durable cumulative fill-notional hardening materialized")
