"""Local persistence (SQLite via SQLAlchemy 2.0).

Stores orders, closed trades, equity snapshots, backtest runs and app
settings (watchlist, risk config, strategy overrides). Everything stays on
this machine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Float, String, Text, Integer, DateTime, create_engine, select, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.domain import Order, TradeRecord


_ACTIVE_DATABASE: Database | None = None


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[datetime]
    mode: Mapped[str] = mapped_column(String(10))
    payload: Mapped[str] = mapped_column(Text)


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    exit_at: Mapped[datetime]
    symbol: Mapped[str] = mapped_column(String(12))
    pnl: Mapped[float]
    mode: Mapped[str] = mapped_column(String(10))
    payload: Mapped[str] = mapped_column(Text)


class EquityRow(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime]
    mode: Mapped[str] = mapped_column(String(10))
    equity: Mapped[float]
    cash: Mapped[float]


class BacktestRow(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime]
    summary: Mapped[str] = mapped_column(Text)  # params + metrics (list view)
    payload: Mapped[str] = mapped_column(Text)  # full result


class SettingRow(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ShadowLedgerRow(Base):
    __tablename__ = "shadow_ledger"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[datetime]
    symbol: Mapped[str] = mapped_column(String(12))
    strategy: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(10))
    mode: Mapped[str] = mapped_column(String(10))
    payload: Mapped[str] = mapped_column(Text)

    # New columns for structured replay outcomes and gating
    replayed: Mapped[bool | None] = mapped_column(nullable=True)
    replay_timestamp: Mapped[datetime | None] = mapped_column(nullable=True)
    simulated_pnl: Mapped[float | None] = mapped_column(nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tp_hit: Mapped[bool | None] = mapped_column(nullable=True)
    sl_hit: Mapped[bool | None] = mapped_column(nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    final_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Lineage & parallel gate evidence (CR-146)
    policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gate_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContextualPerformanceMemoryRow(Base):
    __tablename__ = "contextual_performance_memory"
    strategy: Mapped[str] = mapped_column(String(32), primary_key=True)
    direction: Mapped[str] = mapped_column(String(10), primary_key=True)
    regime: Mapped[str] = mapped_column(String(20), primary_key=True)
    clock_phase: Mapped[str] = mapped_column(String(20), primary_key=True)
    rvol_bucket: Mapped[str] = mapped_column(String(20), primary_key=True)
    vol_bucket: Mapped[str] = mapped_column(String(20), primary_key=True)

    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins_count: Mapped[int] = mapped_column(Integer, default=0)
    total_realized_r: Mapped[float] = mapped_column(Float, default=0.0)
    shrunk_expectancy: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FillProgressRow(Base):
    __tablename__ = "broker_fill_progress"
    mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fill_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    cumulative_qty: Mapped[float] = mapped_column(Float)
    processed_at: Mapped[datetime]


class PortfolioCheckpointRow(Base):
    __tablename__ = "portfolio_checkpoints"
    mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    updated_at: Mapped[datetime]
    payload: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path: Path) -> None:
        global _ACTIVE_DATABASE
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)

        # Self-healing migration for shadow_ledger table columns
        try:
            with self.engine.connect() as conn:
                cursor = conn.exec_driver_sql("PRAGMA table_info(shadow_ledger)")
                existing_cols = {row[1] for row in cursor.fetchall()}

                new_cols = {
                    "replayed": "BOOLEAN",
                    "replay_timestamp": "DATETIME",
                    "simulated_pnl": "FLOAT",
                    "mfe_pct": "FLOAT",
                    "mae_pct": "FLOAT",
                    "exit_reason": "VARCHAR(30)",
                    "tp_hit": "BOOLEAN",
                    "sl_hit": "BOOLEAN",
                    "rejection_reason": "TEXT",
                    "rejection_stage": "VARCHAR(40)",
                    "final_decision": "VARCHAR(20)",
                    "policy_id": "VARCHAR(64)",
                    "parent_policy_id": "VARCHAR(64)",
                    "policy_version": "VARCHAR(20)",
                    "gate_evidence_json": "TEXT",
                }

                needs_commit = False
                for col_name, col_type in new_cols.items():
                    if col_name not in existing_cols:
                        conn.exec_driver_sql(f"ALTER TABLE shadow_ledger ADD COLUMN {col_name} {col_type}")
                        needs_commit = True
                if needs_commit:
                    conn.commit()
        except Exception as exc:
            # Avoid cyclical import for applog at early initialization
            print(f"[Database Migration Warning] Failed to run self-healing migration: {exc}")

        _ACTIVE_DATABASE = self

    @staticmethod
    def active() -> Database | None:
        return _ACTIVE_DATABASE

    def _session(self) -> Session:
        return Session(self.engine)

    # ---- orders / trades / equity ----

    def save_order(self, order: Order, mode: str) -> None:
        with self._session() as s:
            row = s.get(OrderRow, order.id)
            payload = order.model_dump_json()
            if row is None:
                s.add(OrderRow(id=order.id, ts=order.submitted_at, mode=mode, payload=payload))
            else:
                row.payload = payload
            s.commit()

    def get_order(self, order_id: str) -> Order | None:
        with self._session() as s:
            row = s.get(OrderRow, order_id)
            if row is not None:
                try:
                    return Order.model_validate_json(row.payload)
                except Exception:
                    pass
        return None

    def save_trade(self, trade: TradeRecord, mode: str) -> None:
        with self._session() as s:
            s.merge(
                TradeRow(
                    id=trade.id,
                    exit_at=trade.exit_at,
                    symbol=trade.symbol,
                    pnl=trade.pnl,
                    mode=mode,
                    payload=trade.model_dump_json(),
                )
            )
            s.commit()

    def recent_trades(self, mode: str | None = None, limit: int = 200) -> list[dict]:
        with self._session() as s:
            stmt = select(TradeRow).order_by(TradeRow.exit_at.desc()).limit(limit)
            if mode:
                stmt = stmt.where(TradeRow.mode == mode)
            return [json.loads(r.payload) for r in s.scalars(stmt)]

    def save_equity(self, equity: float, cash: float, mode: str, ts: datetime | None = None) -> None:
        with self._session() as s:
            s.add(EquityRow(ts=ts or datetime.now(UTC), mode=mode, equity=equity, cash=cash))
            s.commit()

    def equity_history(self, mode: str, limit: int = 2000) -> list[tuple[datetime, float]]:
        with self._session() as s:
            stmt = (
                select(EquityRow)
                .where(EquityRow.mode == mode)
                .order_by(EquityRow.ts.desc())
                .limit(limit)
            )
            rows = list(s.scalars(stmt))
        return [(r.ts, r.equity) for r in reversed(rows)]

    # ---- durable broker fill / portfolio checkpoint contract ----

    def processed_fill_qty(self, mode: str, account_id: str, fill_key: str) -> float:
        with self._session() as s:
            row = s.get(FillProgressRow, (mode, account_id, fill_key))
            return float(row.cumulative_qty) if row else 0.0

    def get_portfolio_checkpoint(self, mode: str) -> dict | None:
        with self._session() as s:
            row = s.get(PortfolioCheckpointRow, mode)
            return json.loads(row.payload) if row else None

    def has_portfolio_checkpoint(self, mode: str) -> bool:
        with self._session() as s:
            return s.get(PortfolioCheckpointRow, mode) is not None

    def commit_fill_processing(
        self, *, fill_key: str, cumulative_qty: float, mode: str, account_id: str,
        checkpoint: dict, trade: TradeRecord | None,
    ) -> None:
        """Atomically advance a broker execution watermark and portfolio state."""
        with self._session() as s:
            current = s.get(FillProgressRow, (mode, account_id, fill_key))
            if current is not None and float(current.cumulative_qty) >= cumulative_qty - 1e-9:
                return
            s.merge(FillProgressRow(mode=mode, account_id=account_id, fill_key=fill_key,
                                    cumulative_qty=cumulative_qty, processed_at=datetime.now(UTC)))
            s.merge(PortfolioCheckpointRow(mode=mode, updated_at=datetime.now(UTC),
                                           payload=json.dumps(checkpoint, default=str)))
            if trade is not None:
                s.merge(TradeRow(id=trade.id, exit_at=trade.exit_at, symbol=trade.symbol, pnl=trade.pnl,
                                 mode=mode, payload=trade.model_dump_json()))
            s.commit()

    def bootstrap_broker_checkpoint(self, *, mode: str, account_id: str, checkpoint: dict, fill_watermarks: dict[str, float]) -> None:
        """Create the migration baseline once; never advance an existing checkpoint."""
        with self._session() as s:
            if s.get(PortfolioCheckpointRow, mode) is not None:
                return
            s.add(PortfolioCheckpointRow(mode=mode, updated_at=datetime.now(UTC),
                                         payload=json.dumps(checkpoint, default=str)))
            for key, qty in fill_watermarks.items():
                s.merge(FillProgressRow(mode=mode, account_id=account_id, fill_key=key,
                                        cumulative_qty=qty, processed_at=datetime.now(UTC)))
            s.commit()

    # ---- backtests ----

    def save_backtest(self, run_id: str, created_at: datetime, summary: dict, payload: dict) -> None:
        with self._session() as s:
            s.merge(
                BacktestRow(
                    id=run_id,
                    created_at=created_at,
                    summary=json.dumps(summary, default=str),
                    payload=json.dumps(payload, default=str),
                )
            )
            s.commit()

    def list_backtests(self, limit: int = 50) -> list[dict]:
        with self._session() as s:
            stmt = select(BacktestRow).order_by(BacktestRow.created_at.desc()).limit(limit)
            return [
                {"id": r.id, "created_at": r.created_at.isoformat(), **json.loads(r.summary)}
                for r in s.scalars(stmt)
            ]

    def get_backtest(self, run_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(BacktestRow, run_id)
            return json.loads(row.payload) if row else None

    def get_setting(self, key: str, default=None):  # noqa: ANN001, ANN201
        with self._session() as s:
            row = s.get(SettingRow, key)
            return json.loads(row.value) if row else default

    def set_setting(self, key: str, value) -> None:  # noqa: ANN001
        with self._session() as s:
            s.merge(SettingRow(key=key, value=json.dumps(value, default=str)))
            s.commit()

    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:
        with self._session() as s:
            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())
            for row in s.scalars(stmt):
                try:
                    payload = json.loads(row.payload)
                    if payload.get("symbol") == symbol.upper():
                        return payload.get("strategy")
                except Exception:
                    pass
        return None

    def get_latest_order_payload(self, symbol: str, mode: str) -> dict | None:
        with self._session() as s:
            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())
            for row in s.scalars(stmt):
                try:
                    payload = json.loads(row.payload)
                    if payload.get("symbol") == symbol.upper():
                        return payload
                except Exception:
                    pass
        return None

    def save_shadow_signal(self, candidate_id: str, ts: datetime, symbol: str, strategy: str, direction: str, mode: str, payload: dict) -> None:
        # Calculate/update unique/shared/redundant veto attribution based on outcomes if evidence is present
        gate_evidence = payload.get("gate_evidence_json")
        if isinstance(gate_evidence, str):
            try:
                gate_evidence = json.loads(gate_evidence)
            except Exception:
                gate_evidence = None

        if gate_evidence and "vetoes" in gate_evidence:
            vetoes = gate_evidence["vetoes"]
            outcomes = payload.get("outcomes", {})
            realized_r = 0.0
            if outcomes:
                if "15m" in outcomes:
                    realized_r = outcomes["15m"].get("realized_r", 0.0)
                elif "close" in outcomes:
                    realized_r = outcomes["close"].get("realized_r", 0.0)
            from app.strategies.evaluator import ParallelEvidenceEvaluator
            attrib = ParallelEvidenceEvaluator.calculate_veto_attribution(vetoes, realized_r)
            payload.update(attrib)

        with self._session() as s:
            replay_ts = None
            if payload.get("replay_timestamp"):
                try:
                    replay_ts = datetime.fromisoformat(payload["replay_timestamp"])
                except Exception:
                    pass

            s.merge(
                ShadowLedgerRow(
                    id=candidate_id,
                    ts=ts,
                    symbol=symbol,
                    strategy=strategy,
                    direction=direction,
                    mode=mode,
                    payload=json.dumps(payload, default=str),
                    replayed=payload.get("replayed"),
                    replay_timestamp=replay_ts,
                    simulated_pnl=payload.get("simulated_pnl"),
                    mfe_pct=payload.get("mfe_pct"),
                    mae_pct=payload.get("mae_pct"),
                    exit_reason=payload.get("exit_reason"),
                    tp_hit=payload.get("tp_hit"),
                    sl_hit=payload.get("sl_hit"),
                    rejection_reason=payload.get("rejection_reason"),
                    rejection_stage=payload.get("rejection_stage"),
                    final_decision=payload.get("final_decision"),
                    policy_id=payload.get("policy_id"),
                    parent_policy_id=payload.get("parent_policy_id"),
                    policy_version=payload.get("policy_version"),
                    gate_evidence_json=json.dumps(payload["gate_evidence_json"]) if payload.get("gate_evidence_json") is not None else None,
                )
            )
            s.commit()

    def get_shadow_signals(self, mode: str | None = None, limit: int = 1000) -> list[dict]:
        with self._session() as s:
            stmt = select(ShadowLedgerRow).order_by(ShadowLedgerRow.ts.desc()).limit(limit)
            if mode:
                stmt = stmt.where(ShadowLedgerRow.mode == mode)

            results = []
            for r in s.scalars(stmt):
                try:
                    data = json.loads(r.payload)
                    data["replayed"] = r.replayed
                    data["replay_timestamp"] = r.replay_timestamp.isoformat() if r.replay_timestamp else None
                    data["simulated_pnl"] = r.simulated_pnl
                    data["mfe_pct"] = r.mfe_pct
                    data["mae_pct"] = r.mae_pct
                    data["exit_reason"] = r.exit_reason
                    data["tp_hit"] = r.tp_hit
                    data["sl_hit"] = r.sl_hit
                    data["rejection_reason"] = r.rejection_reason
                    data["rejection_stage"] = r.rejection_stage
                    data["final_decision"] = r.final_decision
                    data["policy_id"] = r.policy_id
                    data["parent_policy_id"] = r.parent_policy_id
                    data["policy_version"] = r.policy_version
                    data["gate_evidence_json"] = json.loads(r.gate_evidence_json) if r.gate_evidence_json else None
                    results.append(data)
                except Exception:
                    pass
            return results

    def get_shadow_signal(self, candidate_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(ShadowLedgerRow, candidate_id)
            if row is None:
                return None
            try:
                data = json.loads(row.payload)
                data["replayed"] = row.replayed
                data["replay_timestamp"] = row.replay_timestamp.isoformat() if row.replay_timestamp else None
                data["simulated_pnl"] = row.simulated_pnl
                data["mfe_pct"] = row.mfe_pct
                data["mae_pct"] = row.mae_pct
                data["exit_reason"] = row.exit_reason
                data["tp_hit"] = row.tp_hit
                data["sl_hit"] = row.sl_hit
                data["rejection_reason"] = row.rejection_reason
                data["rejection_stage"] = row.rejection_stage
                data["final_decision"] = row.final_decision
                data["policy_id"] = row.policy_id
                data["parent_policy_id"] = row.parent_policy_id
                data["policy_version"] = row.policy_version
                data["gate_evidence_json"] = json.loads(row.gate_evidence_json) if row.gate_evidence_json else None
                return data
            except Exception:
                return None

    def save_context_aggregates(self, rows: list[dict]) -> None:
        with self._session() as s:
            for row in rows:
                s.merge(
                    ContextualPerformanceMemoryRow(
                        strategy=row["strategy"],
                        direction=row["direction"],
                        regime=row["regime"],
                        clock_phase=row["clock_phase"],
                        rvol_bucket=row["rvol_bucket"],
                        vol_bucket=row["vol_bucket"],
                        trades_count=row["trades_count"],
                        wins_count=row["wins_count"],
                        total_realized_r=row["total_realized_r"],
                        shrunk_expectancy=row["shrunk_expectancy"],
                        updated_at=datetime.utcnow(),
                    )
                )
            s.commit()

    def get_context_aggregate(self, key_dict: dict) -> dict | None:
        with self._session() as s:
            row = s.get(
                ContextualPerformanceMemoryRow,
                (
                    key_dict["strategy"],
                    key_dict["direction"],
                    key_dict["regime"],
                    key_dict["clock_phase"],
                    key_dict["rvol_bucket"],
                    key_dict["vol_bucket"],
                ),
            )
            if row is None:
                return None
            return {
                "strategy": row.strategy,
                "direction": row.direction,
                "regime": row.regime,
                "clock_phase": row.clock_phase,
                "rvol_bucket": row.rvol_bucket,
                "vol_bucket": row.vol_bucket,
                "trades_count": row.trades_count,
                "wins_count": row.wins_count,
                "total_realized_r": row.total_realized_r,
                "shrunk_expectancy": row.shrunk_expectancy,
                "updated_at": row.updated_at,
            }

    def clear_context_aggregates(self) -> None:
        with self._session() as s:
            s.execute(delete(ContextualPerformanceMemoryRow))
            s.commit()

    def close(self) -> None:
        global _ACTIVE_DATABASE
        if _ACTIVE_DATABASE is self:
            _ACTIVE_DATABASE = None
        self.engine.dispose()

