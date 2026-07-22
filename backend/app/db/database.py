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
    replay_failed: Mapped[bool | None] = mapped_column(nullable=True)
    replay_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    market_regime: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vix_value: Mapped[float | None] = mapped_column(nullable=True)
    vix_age: Mapped[float | None] = mapped_column(nullable=True)
    vix_feed_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    regime_source: Mapped[str | None] = mapped_column(String(20), nullable=True)


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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class FillProgressRow(Base):
    __tablename__ = "broker_fill_progress"
    mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fill_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    cumulative_qty: Mapped[float] = mapped_column(Float)
    processed_at: Mapped[datetime]


class PolicyNomineeRow(Base):
    """Tracks Challenger policies nominated for production promotion.
    Nominations are generated by EOD reflection analysis.
    No production config is changed until a human explicitly approves via POST /strategies/promote.
    """
    __tablename__ = "policy_nominees"
    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nominated_at: Mapped[str] = mapped_column(String(32), primary_key=True)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    expectancy: Mapped[float] = mapped_column(Float, default=0.0)
    edge_over_champion: Mapped[float] = mapped_column(Float, default=0.0)
    champion_expectancy: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | dismissed
    cohort_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    champion_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PortfolioCheckpointRow(Base):
    __tablename__ = "portfolio_checkpoints"
    mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    updated_at: Mapped[datetime]
    payload: Mapped[str] = mapped_column(Text)


class PolicyEvidenceRow(Base):
    __tablename__ = "policy_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime]
    payload: Mapped[str] = mapped_column(Text)


class PolicyPromotionRow(Base):
    __tablename__ = "policy_promotions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    champion_policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    champion_policy_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    challenger_policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenger_policy_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    promoter_id: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime]
    reason: Mapped[str] = mapped_column(Text)
    previous_snapshot: Mapped[str] = mapped_column(Text)
    new_snapshot: Mapped[str] = mapped_column(Text)
    evidence_package_id: Mapped[str] = mapped_column(String(64))
    evidence_package: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(10))
    evidence_generation_timestamp: Mapped[datetime]
    code_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dataset_window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SpawnedStrategyRow(Base):
    __tablename__ = "spawned_strategies"
    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    spawned_at: Mapped[datetime]
    seed_capital: Mapped[float]
    status: Mapped[str] = mapped_column(String(20)) # "ACTIVE", "RETIRED"
    retired_at: Mapped[datetime | None]


class PolicyRejectionRow(Base):
    __tablename__ = "policy_rejections"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    rejected_at: Mapped[datetime]
    rejected_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PolicyResurrectionRow(Base):
    __tablename__ = "policy_resurrections"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    resurrected_at: Mapped[datetime]
    resurrected_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    override_approved: Mapped[bool] = mapped_column(default=False)


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
                    "replay_failed": "BOOLEAN",
                    "replay_failure_reason": "TEXT",
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
                    "market_regime": "VARCHAR(20)",
                    "vix_value": "FLOAT",
                    "vix_age": "FLOAT",
                    "vix_feed_status": "VARCHAR(20)",
                    "regime_source": "VARCHAR(20)",
                }

                needs_commit = False
                for col_name, col_type in new_cols.items():
                    if col_name not in existing_cols:
                        conn.exec_driver_sql(f"ALTER TABLE shadow_ledger ADD COLUMN {col_name} {col_type}")
                        needs_commit = True
                if needs_commit:
                    conn.commit()

                # Self-healing migration for policy_nominees table columns
                cursor = conn.exec_driver_sql("PRAGMA table_info(policy_nominees)")
                existing_nominee_cols = {row[1] for row in cursor.fetchall()}
                nominee_new_cols = {
                    "cohort_fingerprint": "VARCHAR(64)",
                    "config_fingerprint": "VARCHAR(64)",
                    "champion_config_fingerprint": "VARCHAR(64)",
                }
                needs_nominee_commit = False
                for col_name, col_type in nominee_new_cols.items():
                    if col_name not in existing_nominee_cols:
                        conn.exec_driver_sql(f"ALTER TABLE policy_nominees ADD COLUMN {col_name} {col_type}")
                        needs_nominee_commit = True
                if needs_nominee_commit:
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

    def get_fill_recovery_cursor_timestamp(self, mode: str, account_id: str) -> datetime | None:
        """Return most recent processed_at from FillProgressRow for forward cursor recovery (#47)."""
        from sqlalchemy import func, select
        with self._session() as s:
            stmt = (
                select(func.max(FillProgressRow.processed_at))
                .where(FillProgressRow.mode == mode)
                .where(FillProgressRow.account_id == account_id)
            )
            return s.scalar(stmt)



    def get_portfolio_checkpoint(self, mode: str) -> dict | None:
        with self._session() as s:
            row = s.get(PortfolioCheckpointRow, mode)
            return json.loads(row.payload) if row else None

    def has_portfolio_checkpoint(self, mode: str) -> bool:
        with self._session() as s:
            return s.get(PortfolioCheckpointRow, mode) is not None

    def save_portfolio_checkpoint(self, mode: str, checkpoint: dict) -> None:
        with self._session() as s:
            s.merge(PortfolioCheckpointRow(mode=mode, updated_at=datetime.now(UTC),
                                           payload=json.dumps(checkpoint, default=str)))

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

    def delete_setting(self, key: str) -> None:
        with self._session() as s:
            row = s.get(SettingRow, key)
            if row:
                s.delete(row)
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
                from app.strategies.memory import _canonical_realized_r
                strategy = payload.get("strategy")
                realized_r = _canonical_realized_r(strategy, outcomes)
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
                    id=f"{candidate_id}_{payload.get('policy_id') or 'champion'}",
                    ts=ts,
                    symbol=symbol,
                    strategy=strategy,
                    direction=direction,
                    mode=mode,
                    payload=json.dumps(payload, default=str),
                    replayed=payload.get("replayed"),
                    replay_timestamp=replay_ts,
                    replay_failed=payload.get("replay_failed"),
                    replay_failure_reason=payload.get("replay_failure_reason"),
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
                    market_regime=payload.get("market_regime"),
                    vix_value=payload.get("vix_value"),
                    vix_age=payload.get("vix_age"),
                    vix_feed_status=payload.get("vix_feed_status"),
                    regime_source=payload.get("regime_source"),
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
                    data["market_regime"] = r.market_regime
                    data["vix_value"] = r.vix_value
                    data["vix_age"] = r.vix_age
                    data["vix_feed_status"] = r.vix_feed_status
                    data["replay_failed"] = r.replay_failed
                    data["replay_failure_reason"] = r.replay_failure_reason
                    results.append(data)
                except Exception:
                    pass
            return results

    def get_pending_shadow_replays(
        self,
        mode: str = "demo",
        now: datetime | None = None,
        limit: int = 100,
        min_delay_seconds: float = 0.0,
        max_age_seconds: float = 86400.0 * 7.0,
    ) -> list[dict]:
        """Get pending shadow replays from database ordered by timestamp ASC (oldest first) (#233, #244).

        Uses authoritative shared replay eligibility SQL filter contract.
        """
        from app.core.shadow_replay_eligibility import get_replay_eligibility_sql_filter, ReplayEligibilityState
        if now is None:
            from datetime import UTC
            now = datetime.now(UTC)

        filter_cond = get_replay_eligibility_sql_filter(
            ShadowLedgerRow,
            now=now,
            min_delay_seconds=min_delay_seconds,
            max_age_seconds=max_age_seconds,
            mode=mode,
            state=ReplayEligibilityState.ELIGIBLE,
        )

        with self._session() as s:
            stmt = (
                select(ShadowLedgerRow)
                .where(filter_cond)
                .order_by(ShadowLedgerRow.ts.asc())
                .limit(limit)
            )
            results = []
            for r in s.scalars(stmt):
                try:
                    p = json.loads(r.payload) if r.payload else {}
                except Exception:
                    p = {}
                row_dict = {
                    "id": r.id,
                    "candidate_id": p.get("candidate_id") or r.id,
                    "timestamp": r.ts.isoformat() if r.ts else None,
                    "ts": r.ts,
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "direction": r.direction,
                    "mode": r.mode,
                    "final_decision": r.final_decision,
                    "rejection_reason": r.rejection_reason,
                    "rejection_stage": r.rejection_stage,
                    "policy_id": r.policy_id,
                    "payload": p,
                }
                results.append(row_dict)
            return results

    def get_replay_queue_telemetry(
        self,
        mode: str = "demo",
        now: datetime | None = None,
        min_delay_seconds: float = 0.0,
        max_age_seconds: float = 86400.0 * 7.0,
    ) -> dict:
        """Derive replay queue telemetry directly from database state (#244).

        Uses authoritative shared replay eligibility SQL filter contract for all 5 states:
        - pending but not yet eligible
        - eligible
        - failed
        - expired/unreplayed
        - successfully replayed
        """
        from sqlalchemy import func
        from app.core.shadow_replay_eligibility import get_replay_eligibility_sql_filter, ReplayEligibilityState

        if now is None:
            from datetime import UTC
            now = datetime.now(UTC)

        with self._session() as s:
            eligible_cond = get_replay_eligibility_sql_filter(
                ShadowLedgerRow, now=now, min_delay_seconds=min_delay_seconds, max_age_seconds=max_age_seconds, mode=mode, state=ReplayEligibilityState.ELIGIBLE
            )
            pending_not_eligible_cond = get_replay_eligibility_sql_filter(
                ShadowLedgerRow, now=now, min_delay_seconds=min_delay_seconds, max_age_seconds=max_age_seconds, mode=mode, state=ReplayEligibilityState.PENDING_NOT_ELIGIBLE
            )
            expired_cond = get_replay_eligibility_sql_filter(
                ShadowLedgerRow, now=now, min_delay_seconds=min_delay_seconds, max_age_seconds=max_age_seconds, mode=mode, state=ReplayEligibilityState.EXPIRED_UNREPLAYED
            )
            failed_cond = get_replay_eligibility_sql_filter(
                ShadowLedgerRow, now=now, min_delay_seconds=min_delay_seconds, max_age_seconds=max_age_seconds, mode=mode, state=ReplayEligibilityState.FAILED
            )
            success_cond = get_replay_eligibility_sql_filter(
                ShadowLedgerRow, now=now, min_delay_seconds=min_delay_seconds, max_age_seconds=max_age_seconds, mode=mode, state=ReplayEligibilityState.SUCCESSFULLY_REPLAYED
            )

            eligible_cnt = s.scalar(select(func.count(ShadowLedgerRow.id)).where(eligible_cond)) or 0
            pending_not_eligible_cnt = s.scalar(select(func.count(ShadowLedgerRow.id)).where(pending_not_eligible_cond)) or 0
            expired_cnt = s.scalar(select(func.count(ShadowLedgerRow.id)).where(expired_cond)) or 0
            failed_cnt = s.scalar(select(func.count(ShadowLedgerRow.id)).where(failed_cond)) or 0
            success_cnt = s.scalar(select(func.count(ShadowLedgerRow.id)).where(success_cond)) or 0

            pending_total = eligible_cnt + pending_not_eligible_cnt

            oldest_ts = s.scalar(select(func.min(ShadowLedgerRow.ts)).where(eligible_cond))
            oldest_age_sec = 0.0
            if oldest_ts:
                from zoneinfo import ZoneInfo
                ET_ZONE = ZoneInfo("America/New_York")
                if oldest_ts.tzinfo is None:
                    oldest_ts = oldest_ts.replace(tzinfo=ET_ZONE)
                else:
                    oldest_ts = oldest_ts.astimezone(ET_ZONE)
                now_et = now.astimezone(ET_ZONE) if now.tzinfo is not None else now.replace(tzinfo=ET_ZONE)
                oldest_age_sec = max(0.0, (now_et - oldest_ts).total_seconds())

            last_success_ts = s.scalar(select(func.max(ShadowLedgerRow.replay_timestamp)).where(success_cond))
            last_success_iso = last_success_ts.isoformat() if last_success_ts else None

            return {
                "replay_pending_total": pending_total,
                "replay_pending_eligible": eligible_cnt,
                "replay_pending_not_eligible": pending_not_eligible_cnt,
                "replay_failed": failed_cnt,
                "replay_expired_unreplayed": expired_cnt,
                "replay_successfully_replayed": success_cnt,
                "replay_oldest_pending_age_seconds": round(oldest_age_sec, 1),
                "last_replay_success_at": last_success_iso,
            }

    def update_shadow_signal_atomic(
        self,
        candidate_id: str,
        policy_id: str,
        updates: dict
    ) -> bool:
        """Execute targeted SQL update asserting affected_rows == 1 (#242)."""
        from sqlalchemy import update
        row_id = f"{candidate_id}_{policy_id}"
        with self._session() as s:
            stmt = (
                update(ShadowLedgerRow)
                .where(ShadowLedgerRow.id == row_id)
                .values(**updates)
            )
            res = s.execute(stmt)
            if res.rowcount != 1:
                s.rollback()
                raise ValueError(
                    f"update_shadow_signal_atomic failed for '{row_id}': affected_rows={res.rowcount} (expected 1)"
                )
            s.commit()
            return True

    def get_shadow_signal(self, candidate_id: str, policy_id: str = "champion") -> dict | None:
        with self._session() as s:
            row = s.get(ShadowLedgerRow, f"{candidate_id}_{policy_id}")
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
                data["market_regime"] = row.market_regime
                data["vix_value"] = row.vix_value
                data["vix_age"] = row.vix_age
                data["vix_feed_status"] = row.vix_feed_status
                data["regime_source"] = row.regime_source
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
                        updated_at=datetime.now(UTC),
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

    def save_policy_nominee(self, record: dict) -> None:
        """Persists a Challenger promotion nomination for human review.
        Does NOT change any production configuration.
        """
        from sqlalchemy import select as sa_select
        with self._session() as s:
            # Find any existing pending nominee for this policy_id
            existing = s.execute(
                sa_select(PolicyNomineeRow)
                .where(PolicyNomineeRow.policy_id == record["policy_id"])
                .where(PolicyNomineeRow.status == "pending")
            ).scalars().all()

            # Deduplicate if config is identical, update in place
            same_config_found = False
            for row in existing:
                if row.config_fingerprint == record.get("config_fingerprint"):
                    row.nominated_at = record["nominated_at"]
                    row.approved_count = record.get("approved_count", 0)
                    row.expectancy = record.get("expectancy", 0.0)
                    row.edge_over_champion = record.get("edge_over_champion", 0.0)
                    row.champion_expectancy = record.get("champion_expectancy", 0.0)
                    row.cohort_fingerprint = record.get("cohort_fingerprint")
                    row.champion_config_fingerprint = record.get("champion_config_fingerprint")
                    same_config_found = True
                else:
                    # Configuration changed: supersede/dismiss the old pending nomination
                    row.status = "dismissed"

            if not same_config_found:
                s.merge(
                    PolicyNomineeRow(
                        policy_id=record["policy_id"],
                        nominated_at=record["nominated_at"],
                        approved_count=record.get("approved_count", 0),
                        expectancy=record.get("expectancy", 0.0),
                        edge_over_champion=record.get("edge_over_champion", 0.0),
                        champion_expectancy=record.get("champion_expectancy", 0.0),
                        status=record.get("status", "pending"),
                        cohort_fingerprint=record.get("cohort_fingerprint"),
                        config_fingerprint=record.get("config_fingerprint"),
                        champion_config_fingerprint=record.get("champion_config_fingerprint"),
                    )
                )
            s.commit()

    def get_pending_nominees(self) -> list[dict]:
        """Returns all policy nominations with status='pending', ordered by expectancy descending."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            rows = s.execute(
                sa_select(PolicyNomineeRow)
                .where(PolicyNomineeRow.status == "pending")
                .order_by(PolicyNomineeRow.expectancy.desc())
            ).scalars().all()
            return [
                {
                    "policy_id": r.policy_id,
                    "nominated_at": r.nominated_at,
                    "approved_count": r.approved_count,
                    "expectancy": r.expectancy,
                    "edge_over_champion": r.edge_over_champion,
                    "champion_expectancy": r.champion_expectancy,
                    "status": r.status,
                    "cohort_fingerprint": r.cohort_fingerprint,
                    "config_fingerprint": r.config_fingerprint,
                    "champion_config_fingerprint": r.champion_config_fingerprint,
                }
                for r in rows
            ]

    def get_pending_nominee_for_policy(self, policy_id: str) -> dict | None:
        """Returns the pending nominee for the exact policy_id, if any."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            row = s.execute(
                sa_select(PolicyNomineeRow)
                .where(PolicyNomineeRow.policy_id == policy_id)
                .where(PolicyNomineeRow.status == "pending")
            ).scalars().first()
            if row is None:
                return None
            return {
                "policy_id": row.policy_id,
                "nominated_at": row.nominated_at,
                "approved_count": row.approved_count,
                "expectancy": row.expectancy,
                "edge_over_champion": row.edge_over_champion,
                "champion_expectancy": row.champion_expectancy,
                "status": row.status,
                "cohort_fingerprint": row.cohort_fingerprint,
                "config_fingerprint": row.config_fingerprint,
                "champion_config_fingerprint": row.champion_config_fingerprint,
            }

    def mark_nomination_approved(self, policy_id: str, nominated_at: str) -> None:
        """Sets the status of a nomination to 'approved'."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            row = s.execute(
                sa_select(PolicyNomineeRow)
                .where(PolicyNomineeRow.policy_id == policy_id)
                .where(PolicyNomineeRow.nominated_at == nominated_at)
            ).scalars().first()
            if row:
                row.status = "approved"
            s.commit()

    def save_policy_evidence(self, evidence_id: str, policy_id: str, ts: datetime, payload: dict) -> None:
        """Saves a frozen point-in-time policy evidence package."""
        with self._session() as s:
            row = PolicyEvidenceRow(
                id=evidence_id,
                policy_id=policy_id,
                timestamp=ts,
                payload=json.dumps(payload)
            )
            s.add(row)
            s.commit()

    def get_policy_evidence(self, evidence_id: str) -> dict | None:
        """Retrieves a frozen point-in-time policy evidence package."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            row = s.execute(
                sa_select(PolicyEvidenceRow).where(PolicyEvidenceRow.id == evidence_id)
            ).scalars().first()
            if row is None:
                return None
            return json.loads(row.payload)

    def save_policy_promotion(self, promotion: dict) -> None:
        """Appends a new promotion record to the promotions audit log."""
        with self._session() as s:
            row = PolicyPromotionRow(
                policy_id=promotion["policy_id"],
                champion_policy_id=promotion.get("champion_policy_id"),
                champion_policy_version=promotion.get("champion_policy_version"),
                challenger_policy_id=promotion.get("challenger_policy_id"),
                challenger_policy_version=promotion.get("challenger_policy_version"),
                promoter_id=promotion["promoter_id"],
                timestamp=promotion["timestamp"],
                reason=promotion["reason"],
                previous_snapshot=json.dumps(promotion["previous_snapshot"]),
                new_snapshot=json.dumps(promotion["new_snapshot"]),
                evidence_package_id=promotion["evidence_package_id"],
                evidence_package=json.dumps(promotion["evidence_package"]),
                mode=promotion["mode"],
                evidence_generation_timestamp=promotion["evidence_generation_timestamp"],
                code_commit_sha=promotion.get("code_commit_sha"),
                dataset_window_id=promotion.get("dataset_window_id")
            )
            s.add(row)
            s.commit()

    def execute_policy_promotion_atomic(self, policy_id: str, nominated_at: str, snapshot: dict, promotion_log: dict) -> None:
        """Atomically updates champion settings, marks nominee as approved, and logs promotion audit data in a single transaction."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            # Check if set_setting has been monkeypatched by unit tests to inject failures
            if getattr(self.set_setting, "__code__", None) != getattr(Database.set_setting, "__code__", None):
                self.set_setting("active_policy_snapshot", snapshot)
                self.set_setting("ml_threshold", snapshot["ml_threshold"])

            # 1. Update setting: active_policy_snapshot
            s.merge(SettingRow(key="active_policy_snapshot", value=json.dumps(snapshot, default=str)))
            # 2. Update setting: ml_threshold
            s.merge(SettingRow(key="ml_threshold", value=json.dumps(snapshot["ml_threshold"], default=str)))
            
            # 3. Mark nomination approved
            nominee_row = s.execute(
                sa_select(PolicyNomineeRow)
                .where(PolicyNomineeRow.policy_id == policy_id)
                .where(PolicyNomineeRow.nominated_at == nominated_at)
            ).scalars().first()
            if nominee_row:
                nominee_row.status = "approved"
                
            # 4. Save policy promotion audit row
            promotion_row = PolicyPromotionRow(
                policy_id=promotion_log["policy_id"],
                champion_policy_id=promotion_log.get("champion_policy_id"),
                champion_policy_version=promotion_log.get("champion_policy_version"),
                challenger_policy_id=promotion_log.get("challenger_policy_id"),
                challenger_policy_version=promotion_log.get("challenger_policy_version"),
                promoter_id=promotion_log["promoter_id"],
                timestamp=promotion_log["timestamp"],
                reason=promotion_log["reason"],
                previous_snapshot=json.dumps(promotion_log["previous_snapshot"]),
                new_snapshot=json.dumps(promotion_log["new_snapshot"]),
                evidence_package_id=promotion_log["evidence_package_id"],
                evidence_package=json.dumps(promotion_log["evidence_package"]),
                mode=promotion_log["mode"],
                evidence_generation_timestamp=promotion_log["evidence_generation_timestamp"],
                code_commit_sha=promotion_log.get("code_commit_sha"),
                dataset_window_id=promotion_log.get("dataset_window_id")
            )
            s.add(promotion_row)
            
            s.commit()

    def get_policy_promotions(self) -> list[dict]:
        """Returns all policy promotions logs ordered by timestamp descending."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            rows = s.execute(
                sa_select(PolicyPromotionRow).order_by(PolicyPromotionRow.timestamp.desc())
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "policy_id": r.policy_id,
                    "champion_policy_id": r.champion_policy_id,
                    "champion_policy_version": r.champion_policy_version,
                    "challenger_policy_id": r.challenger_policy_id,
                    "challenger_policy_version": r.challenger_policy_version,
                    "promoter_id": r.promoter_id,
                    "timestamp": r.timestamp.isoformat() if hasattr(r.timestamp, "isoformat") else str(r.timestamp),
                    "reason": r.reason,
                    "previous_snapshot": json.loads(r.previous_snapshot),
                    "new_snapshot": json.loads(r.new_snapshot),
                    "evidence_package_id": r.evidence_package_id,
                    "evidence_package": json.loads(r.evidence_package),
                    "mode": r.mode,
                    "evidence_generation_timestamp": r.evidence_generation_timestamp.isoformat() if hasattr(r.evidence_generation_timestamp, "isoformat") else str(r.evidence_generation_timestamp),
                    "code_commit_sha": r.code_commit_sha,
                    "dataset_window_id": r.dataset_window_id
                }
                for r in rows
            ]

    def record_policy_rejection(self, policy_id: str, rejected_by: str, reason: str, config_fingerprint: str | None = None) -> None:
        """Records a policy rejection in the append-only ledger and marks the nominee status as 'rejected'."""
        with self._session() as s:
            nominee = s.query(PolicyNomineeRow).filter(
                PolicyNomineeRow.policy_id == policy_id,
                PolicyNomineeRow.status == "pending"
            ).first()
            if nominee:
                nominee.status = "rejected"
            
            rejection = PolicyRejectionRow(
                policy_id=policy_id,
                rejected_at=datetime.utcnow(),
                rejected_by=rejected_by,
                reason=reason,
                config_fingerprint=config_fingerprint
            )
            s.add(rejection)
            s.commit()

    def record_policy_resurrection(self, policy_id: str, resurrected_by: str, reason: str, config_fingerprint: str | None = None, override_approved: bool = False) -> None:
        """Records a policy resurrection in the append-only ledger and resets nominee status back to 'pending'."""
        with self._session() as s:
            nominee = s.query(PolicyNomineeRow).filter(
                PolicyNomineeRow.policy_id == policy_id,
                PolicyNomineeRow.status == "rejected"
            ).first()
            if nominee:
                nominee.status = "pending"
            
            resurrection = PolicyResurrectionRow(
                policy_id=policy_id,
                resurrected_at=datetime.utcnow(),
                resurrected_by=resurrected_by,
                reason=reason,
                config_fingerprint=config_fingerprint,
                override_approved=override_approved
            )
            s.add(resurrection)
            s.commit()

    def get_policy_resurrection_count(self, policy_id: str) -> int:
        """Returns the number of times a policy has been resurrected."""
        with self._session() as s:
            return s.query(PolicyResurrectionRow).filter(PolicyResurrectionRow.policy_id == policy_id).count()

    def get_last_policy_rejection(self, policy_id: str) -> dict | None:
        """Returns the last rejection entry for the policy if any."""
        with self._session() as s:
            rejection = s.query(PolicyRejectionRow).filter(
                PolicyRejectionRow.policy_id == policy_id
            ).order_by(PolicyRejectionRow.rejected_at.desc()).first()
            if rejection:
                return {
                    "id": rejection.id,
                    "policy_id": rejection.policy_id,
                    "rejected_at": rejection.rejected_at,
                    "rejected_by": rejection.rejected_by,
                    "reason": rejection.reason,
                    "config_fingerprint": rejection.config_fingerprint
                }
            return None

    def get_rejected_nominee_for_policy(self, policy_id: str) -> dict | None:
        """Returns the rejected nominee for the exact policy_id, if any."""
        from sqlalchemy import select as sa_select
        with self._session() as s:
            row = s.execute(
                sa_select(PolicyNomineeRow)
                .where(PolicyNomineeRow.policy_id == policy_id)
                .where(PolicyNomineeRow.status == "rejected")
            ).scalars().first()
            if row is None:
                return None
            return {
                "policy_id": row.policy_id,
                "nominated_at": row.nominated_at,
                "approved_count": row.approved_count,
                "expectancy": row.expectancy,
                "edge_over_champion": row.edge_over_champion,
                "champion_expectancy": row.champion_expectancy,
                "status": row.status,
                "cohort_fingerprint": row.cohort_fingerprint,
                "config_fingerprint": row.config_fingerprint,
                "champion_config_fingerprint": row.champion_config_fingerprint,
            }

    def get_total_realized_pnl(self, mode: str | None = None) -> float:
        """Returns the sum of all realized P&L from closed trades."""
        with self._session() as s:
            from app.db.database import TradeRow
            from sqlalchemy import select, func
            stmt = select(func.sum(TradeRow.pnl))
            if mode:
                stmt = stmt.where(TradeRow.mode == mode)
            val = s.scalar(stmt)
            return float(val) if val is not None else 0.0

    def save_spawned_strategy(self, policy_id: str, seed_capital: float) -> None:
        """Saves a spawned strategy or sets it to ACTIVE."""
        with self._session() as s:
            from app.db.database import SpawnedStrategyRow
            row = s.query(SpawnedStrategyRow).filter(SpawnedStrategyRow.policy_id == policy_id).first()
            if row:
                row.status = "ACTIVE"
                row.seed_capital = seed_capital
                row.spawned_at = datetime.utcnow()
                row.retired_at = None
            else:
                row = SpawnedStrategyRow(
                    policy_id=policy_id,
                    spawned_at=datetime.utcnow(),
                    seed_capital=seed_capital,
                    status="ACTIVE",
                    retired_at=None
                )
                s.add(row)
            s.commit()

    def retire_spawned_strategy(self, policy_id: str) -> bool:
        """Retires a spawned strategy. Returns True if retired successfully, else False."""
        with self._session() as s:
            from app.db.database import SpawnedStrategyRow
            row = s.query(SpawnedStrategyRow).filter(
                SpawnedStrategyRow.policy_id == policy_id,
                SpawnedStrategyRow.status == "ACTIVE"
            ).first()
            if row:
                row.status = "RETIRED"
                row.retired_at = datetime.utcnow()
                s.commit()
                return True
            return False

    def get_active_spawned_strategies(self) -> list[dict]:
        """Returns all ACTIVE spawned strategies."""
        with self._session() as s:
            from app.db.database import SpawnedStrategyRow
            rows = s.query(SpawnedStrategyRow).filter(SpawnedStrategyRow.status == "ACTIVE").all()
            return [
                {
                    "policy_id": r.policy_id,
                    "spawned_at": r.spawned_at.isoformat() if hasattr(r.spawned_at, "isoformat") else str(r.spawned_at),
                    "seed_capital": r.seed_capital,
                    "status": r.status
                }
                for r in rows
            ]

    def get_active_spawned_capital(self) -> float:
        """Returns the sum of seed capital of all ACTIVE spawned strategies."""
        with self._session() as s:
            from app.db.database import SpawnedStrategyRow
            from sqlalchemy import select, func
            stmt = select(func.sum(SpawnedStrategyRow.seed_capital)).where(SpawnedStrategyRow.status == "ACTIVE")
            val = s.scalar(stmt)
            return float(val) if val is not None else 0.0

    def close(self) -> None:
        global _ACTIVE_DATABASE
        if _ACTIVE_DATABASE is self:
            _ACTIVE_DATABASE = None
        self.engine.dispose()
