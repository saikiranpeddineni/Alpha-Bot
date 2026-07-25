"""ReplayLedger -- Multi-stage telemetry & audit event recorder for replay.

Captures un-truncated events across every stage of the decision path:
  - Scanner candidates
  - Strategy signals
  - ML & Meta probabilities
  - Spawn decisions
  - Portfolio allocations
  - Risk decisions
  - Orders & fills
  - Position state transitions
  - Multi-stage state hashes
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any


class ReplayLedger:
    def __init__(self, replay_id: str) -> None:
        self.replay_id = replay_id
        self.candidates: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []
        self.ml_decisions: list[dict[str, Any]] = []
        self.spawn_decisions: list[dict[str, Any]] = []
        self.risk_decisions: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []

    def record_event(self, event_type: str, timestamp: datetime, symbol: str, details: dict[str, Any]) -> None:
        ts_iso = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        event = {
            "replay_id": self.replay_id,
            "timestamp": ts_iso,
            "event_type": event_type,
            "symbol": symbol,
            "details": details,
        }
        self.timeline.append(event)

        if event_type == "SCANNER_CANDIDATE":
            self.candidates.append(event)
        elif event_type == "STRATEGY_SIGNAL":
            self.signals.append(event)
        elif event_type == "ML_DECISION":
            self.ml_decisions.append(event)
        elif event_type == "SPAWN_DECISION":
            self.spawn_decisions.append(event)
        elif event_type == "RISK_DECISION":
            self.risk_decisions.append(event)
        elif event_type == "ORDER_SUBMITTED":
            self.orders.append(event)
        elif event_type == "FILL_EXECUTED":
            self.fills.append(event)
        elif event_type == "TRADE_CLOSED":
            self.trades.append(event)

    def record_equity(self, timestamp: datetime, equity: float, cash: float, buying_power: float, open_positions: int) -> None:
        ts_iso = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        self.equity_curve.append({
            "timestamp": ts_iso,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "open_positions": open_positions,
        })

    def compute_stage_hashes(self) -> dict[str, str]:
        """Compute SHA256 hashes for each stage of the decision path."""
        def sanitize_item(item: Any) -> Any:
            if isinstance(item, dict):
                return {k: sanitize_item(v) for k, v in item.items() if k != "replay_id"}
            if isinstance(item, list):
                return [sanitize_item(i) for i in item]
            return item

        def hash_list(data: list) -> str:
            sanitized = sanitize_item(data)
            payload = json.dumps(sanitized, sort_keys=True, default=str)
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

        return {
            "scanner_hash": hash_list(self.candidates),
            "strategy_hash": hash_list(self.signals),
            "ml_hash": hash_list(self.ml_decisions),
            "spawn_hash": hash_list(self.spawn_decisions),
            "risk_hash": hash_list(self.risk_decisions),
            "order_hash": hash_list(self.orders),
            "fill_hash": hash_list(self.fills),
            "trade_hash": hash_list(self.trades),
            "equity_hash": hash_list(self.equity_curve),
            "overall_state_hash": hash_list(self.timeline),
        }
