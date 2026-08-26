"""Local persistence (SQLite via SQLAlchemy 2.0).

Stores orders, closed trades, equity snapshots, backtest runs and app
settings (watchlist, risk config, strategy overrides). Everything stays on
this machine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Float, String, Text, create_engine, select
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


class FillProgressRow(Base):
    __tablename__ = "broker_fill_progress"
    mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fill_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    cumulative_qty: Mapped[float] = mapped_column(Float)
    cumulative_notional: Mapped[float] = mapped_column(Float, default=0.0)
    processed_at: Mapped[datetime]


class PortfolioCheckpointRow(Base):
    __tablename__ = "portfolio_checkpoints"
    mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    updated_at: Mapped[datetime]
    payload: Mapped[str] = mapped_column(Text)


class PisProfitMemoryRow(Base):
    __tablename__ = "pis_profit_memory"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), primary_key=True)
    updated_at: Mapped[datetime]
    payload: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path: Path) -> None:
        global _ACTIVE_DATABASE
        path.parent.mkdir(parents=True, exist_ok=True)
        from sqlalchemy import pool
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30.0},
            poolclass=pool.QueuePool,
            pool_size=15,
            max_overflow=25
        )
        with self.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            conn.exec_driver_sql("PRAGMA busy_timeout=30000")
        Base.metadata.create_all(self.engine)

        # Backward-compatible migration for durable partial-fill pricing.
        try:
            with self.engine.connect() as conn:
                cursor = conn.exec_driver_sql("PRAGMA table_info(broker_fill_progress)")
                fill_progress_cols = {row[1] for row in cursor.fetchall()}
                if "cumulative_notional" not in fill_progress_cols:
                    conn.exec_driver_sql(
                        "ALTER TABLE broker_fill_progress ADD COLUMN cumulative_notional FLOAT DEFAULT 0.0"
                    )
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

    def get_recent_trade_contexts(self, lookback_days: int, mode: str | None = None) -> list[dict]:
        """Closed trades within the lookback window for performance learning.

        Returns lightweight context dicts: {symbol, strategy, r_multiple,
        exit_at_ts: datetime}. Trades without a measurable R multiple are
        excluded (the governor reasons in R space only).
        """
        cutoff = datetime.now(UTC) - timedelta(days=max(1, int(lookback_days)))
        out: list[dict] = []
        with self._session() as s:
            stmt = select(TradeRow).where(TradeRow.exit_at >= cutoff).order_by(TradeRow.exit_at.desc())
            if mode:
                stmt = stmt.where(TradeRow.mode == mode)
            for r in s.scalars(stmt):
                try:
                    p = json.loads(r.payload)
                except Exception:
                    continue
                rmult = p.get("r_multiple")
                if rmult is None:
                    continue
                out.append(
                    {
                        "symbol": p.get("symbol") or r.symbol,
                        "strategy": p.get("strategy", ""),
                        "r_multiple": rmult,
                        "exit_at_ts": r.exit_at,
                    }
                )
        return out

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

    def processed_fill_notional(self, mode: str, account_id: str, fill_key: str) -> float:
        with self._session() as s:
            row = s.get(FillProgressRow, (mode, account_id, fill_key))
            return float(row.cumulative_notional or 0.0) if row else 0.0

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
            s.commit()

    def get_pis_profit_memory(self, mode: str, symbol: str) -> dict | None:
        with self._session() as s:
            row = s.get(PisProfitMemoryRow, (symbol, mode))
            return json.loads(row.payload) if row else None

    def save_pis_profit_memory(self, mode: str, symbol: str, payload: dict) -> None:
        with self._session() as s:
            s.merge(PisProfitMemoryRow(mode=mode, symbol=symbol, updated_at=datetime.now(UTC),
                                       payload=json.dumps(payload, default=str)))
            s.commit()

    def delete_pis_profit_memory(self, mode: str, symbol: str) -> None:
        with self._session() as s:
            row = s.get(PisProfitMemoryRow, (symbol, mode))
            if row:
                s.delete(row)
                s.commit()

    def commit_fill_processing(
        self, *, fill_key: str, cumulative_qty: float, cumulative_notional: float = 0.0, mode: str, account_id: str,
        checkpoint: dict, trade: TradeRecord | None,
    ) -> None:
        """Atomically advance a broker execution watermark and portfolio state."""
        with self._session() as s:
            current = s.get(FillProgressRow, (mode, account_id, fill_key))
            if current is not None and float(current.cumulative_qty) >= cumulative_qty - 1e-9:
                return
            s.merge(FillProgressRow(mode=mode, account_id=account_id, fill_key=fill_key,
                                    cumulative_qty=cumulative_qty, cumulative_notional=cumulative_notional,
                                    processed_at=datetime.now(UTC)))
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

    def get_latest_entry_order_payload(self, symbol: str, mode: str) -> dict | None:
        """Return the newest true entry order for an active position.

        Protective/recovery orders are operational children of an existing position;
        they must never become the position's owning strategy on restart.
        """
        symbol = symbol.upper()
        non_owning_strategies = {"emergency_protection"}
        with self._session() as s:
            stmt = select(OrderRow).where(OrderRow.mode == mode).order_by(OrderRow.ts.desc())
            for row in s.scalars(stmt):
                try:
                    payload = json.loads(row.payload)
                    if str(payload.get("symbol", "")).upper() != symbol:
                        continue
                    strategy = str(payload.get("strategy") or "").strip()
                    leg = str(payload.get("leg") or "entry").lower()
                    if strategy in non_owning_strategies or leg != "entry":
                        continue
                    return payload
                except Exception:
                    pass
        return None

    def get_latest_entry_order_strategy(self, symbol: str, mode: str) -> str | None:
        payload = self.get_latest_entry_order_payload(symbol, mode)
        return payload.get("strategy") if payload else None

    def get_latest_order_strategy(self, symbol: str, mode: str) -> str | None:
        payload = self.get_latest_entry_order_payload(symbol, mode)
        return payload.get("strategy") if payload else None

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

    def get_total_realized_pnl(self, mode: str | None = None) -> float:
        """Returns the sum of all realized P&L from closed trades."""
        with self._session() as s:
            from sqlalchemy import func, select

            from app.db.database import TradeRow
            stmt = select(func.sum(TradeRow.pnl))
            if mode:
                stmt = stmt.where(TradeRow.mode == mode)
            val = s.scalar(stmt)
            return float(val) if val is not None else 0.0

    def close(self) -> None:
        global _ACTIVE_DATABASE
        if _ACTIVE_DATABASE is self:
            _ACTIVE_DATABASE = None
        self.engine.dispose()

