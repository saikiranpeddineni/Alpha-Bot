"""Core domain models shared across the engine, brokers, strategies and API.

Pydantic models double as the wire format for the REST/WebSocket layer, so
the frontend consumes exactly what the engine produces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class SignalAction(StrEnum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"


class Signal(BaseModel):
    """A trading intent produced by a strategy, before AI gating and risk sizing."""

    symbol: str
    action: SignalAction
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: str
    reason: str
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    ts: datetime = Field(default_factory=utcnow)
    decision_bar_timestamp_utc: datetime | None = None
    # Enriched by the AI signal pipeline:
    ml_probability: float | None = None
    regime: Regime | None = None
    score: float | None = None
    candidate_id: str | None = None
    strategy_params: dict | None = None


class OrderRequest(BaseModel):
    """A risk-approved instruction handed to a broker."""

    symbol: str
    side: OrderSide
    qty: float = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str = Field(default_factory=new_id)
    strategy: str = "manual"
    reason: str = ""
    time_in_force: str = "day"
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None


class Order(BaseModel):
    id: str = Field(default_factory=new_id)
    client_order_id: str = ""
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str = "manual"
    reason: str = ""
    submitted_at: datetime = Field(default_factory=utcnow)
    filled_at: datetime | None = None
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None
    # Distinguishes entries from bracket children in the paper simulator
    parent_id: str | None = None
    leg: str = "entry"  # entry | stop | target | exit


class Position(BaseModel):
    symbol: str
    qty: float  # positive = long, negative = short
    avg_entry_price: float
    market_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str = "manual"
    opened_at: datetime = Field(default_factory=utcnow)

    @property
    def side(self) -> str:
        return "long" if self.qty >= 0 else "short"

    @property
    def market_value(self) -> float:
        return self.qty * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.market_price - self.avg_entry_price) * self.qty

    @property
    def unrealized_pnl_pct(self) -> float:
        basis = abs(self.qty) * self.avg_entry_price
        return (self.unrealized_pnl / basis * 100.0) if basis else 0.0

    def to_payload(self) -> dict:
        d = self.model_dump(mode="json")
        d.update(
            side=self.side,
            market_value=round(self.market_value, 2),
            unrealized_pnl=round(self.unrealized_pnl, 2),
            unrealized_pnl_pct=round(self.unrealized_pnl_pct, 3),
        )
        return d


class TradeRecord(BaseModel):
    """A closed round trip."""

    id: str = Field(default_factory=new_id)
    symbol: str
    side: str  # long | short
    qty: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    pnl: float
    pnl_pct: float
    r_multiple: float | None = None
    strategy: str = "manual"
    exit_reason: str = ""

    # Observability & Excursion Metrics
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None
    order_submitted_timestamp_utc: datetime | None = None
    entry_fill_timestamp_utc: datetime | None = None
    exit_signal_timestamp_utc: datetime | None = None
    exit_fill_timestamp_utc: datetime | None = None
    mfe: float | None = None
    mae: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    capture_efficiency: float | None = None
    hold_bars: int = 0


class AccountSnapshot(BaseModel):
    equity: float
    cash: float
    buying_power: float
    ts: datetime = Field(default_factory=utcnow)


class Quote(BaseModel):
    symbol: str
    price: float
    change_pct: float = 0.0
    volume: float = 0.0
    ts: datetime = Field(default_factory=utcnow)


class InsightItem(BaseModel):
    id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=utcnow)
    kind: str  # regime | signal | risk | performance | model | narrative
    title: str
    detail: str
    symbol: str | None = None
    severity: str = "info"  # info | success | warning | danger


class EngineState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    HALTED = "halted"  # kill switch tripped; manual reset required
