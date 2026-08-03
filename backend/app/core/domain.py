"""Core domain models shared across the engine, brokers, strategies and API.

Pydantic models double as the wire format for the REST/WebSocket layer, so
the frontend consumes exactly what the engine produces.
"""
from __future__ import annotations
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field, model_validator

import contextvars
from typing import Callable

_mock_utcnow: contextvars.ContextVar[Callable[[], datetime] | None] = contextvars.ContextVar("mock_utcnow", default=None)

def utcnow() -> datetime:
    mock = _mock_utcnow.get()
    if mock is not None:
        return mock()
    return datetime.now(UTC)

def new_id() -> str:
    return uuid.uuid4().hex

class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"
class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
class OrderStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
class ProtectionState(StrEnum):
    UNPROTECTED = "unprotected"
    AWAITING_CANCEL_CONFIRMATION = "awaiting_cancel_confirmation"
    AWAITING_STOP_CONFIRMATION = "awaiting_stop_confirmation"
    PROTECTED = "protected"
    RECOVERY_REJECTED = "recovery_rejected"
    AMBIGUOUS = "ambiguous"
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
    ml_probability: float | None = None
    ml_gate_decision: str | None = None
    regime: Regime | None = None
    score: float | None = None
    candidate_id: str | None = None
    strategy_params: dict | None = None
    parent_candidate_id: str | None = None
    risk_multiplier: float = 1.0
    discovery_source: str | None = None
    scanner_score: float | None = None
    scanner_auc: float | None = None

    @property
    def side(self) -> str:
        if self.action == SignalAction.ENTER_LONG:
            return "long"
        if self.action == SignalAction.ENTER_SHORT:
            return "short"
        return "exit"

    @property
    def ml_confidence(self) -> float | None:
        return self.ml_probability

    @property
    def trade_score(self) -> float | None:
        return float(self.score) * 100.0 if self.score is not None else None

class OrderRequest(BaseModel):
    """A risk-approved instruction handed to a broker."""
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str = Field(default_factory=new_id)
    strategy: str = "manual"
    reason: str = ""
    time_in_force: str = "day"
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None
    @model_validator(mode="after")
    def validate_execution_prices(self) -> OrderRequest:
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if self.order_type == OrderType.MARKET:
            if self.limit_price is not None or self.stop_price is not None:
                raise ValueError("MARKET orders cannot specify limit_price or stop_price")
        elif self.order_type == OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError("LIMIT requires positive limit_price")
            if self.stop_price is not None:
                raise ValueError("LIMIT orders cannot specify stop_price")
        elif self.order_type == OrderType.STOP:
            if self.stop_price is None or self.stop_price <= 0:
                raise ValueError("STOP requires positive stop_price")
            if self.limit_price is not None:
                raise ValueError("STOP orders cannot specify limit_price")
        elif self.order_type == OrderType.STOP_LIMIT:
            if self.stop_price is None or self.stop_price <= 0:
                raise ValueError("STOP_LIMIT requires positive stop_price")
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError("STOP_LIMIT requires positive limit_price")
        return self

class Order(BaseModel):
    id: str = Field(default_factory=new_id)
    client_order_id: str = ""
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    stop_triggered: bool = False
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    order_class: str | None = None
    parent_id: str | None = None
    leg: str = "entry"
    strategy: str = "manual"
    reason: str = ""
    submitted_at: datetime | None = Field(default_factory=utcnow)
    filled_at: datetime | None = None
    signal_timestamp_utc: datetime | None = None
    decision_bar_timestamp_utc: datetime | None = None
    @property
    def remaining_qty(self) -> float:
        return max(0.0, float(self.qty) - float(self.filled_qty or 0.0))

class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float
    market_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str = "manual"
    opened_at: datetime | None = None
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
        d.update(side=self.side, market_value=round(self.market_value, 2), unrealized_pnl=round(self.unrealized_pnl, 2), unrealized_pnl_pct=round(self.unrealized_pnl_pct, 3))
        return d

class TradeRecord(BaseModel):
    """A closed round trip."""
    id: str = Field(default_factory=new_id)
    symbol: str
    side: str
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
    candidate_id: str | None = None
    initial_stop: float | None = None
    initial_risk: float | None = None
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
    metadata: dict[str, Any] | None = None

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
    kind: str
    title: str
    detail: str
    symbol: str | None = None
    severity: str = "info"
class EngineState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    HALTED = "halted"
    SAFE_MODE = "safe_mode"
class SafetyDecision(BaseModel):
    system_state: str
    market_risk_state: str
    allow_new_entries: bool
    risk_multiplier: float
    allowed_strategies: list[str]
    require_exit_only: bool
    halt_reason: str | None = None
