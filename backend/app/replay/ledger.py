"""Replay audit ledger with deterministic, stage-specific hashes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


class ReplayLedger:
    def __init__(self, replay_id: str) -> None:
        self.replay_id = replay_id
        self.production_ticks = 0
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
        event = {"replay_id": self.replay_id, "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp),
                 "event_type": event_type, "symbol": symbol, "details": details}
        self.timeline.append(event)
        mapping = {
            "SCANNER_CANDIDATE": self.candidates, "STRATEGY_SIGNAL": self.signals,
            "ML_DECISION": self.ml_decisions, "SPAWN_DECISION": self.spawn_decisions,
            "RISK_DECISION": self.risk_decisions, "ORDER_SUBMITTED": self.orders,
            "FILL_EXECUTED": self.fills, "TRADE_CLOSED": self.trades,
        }
        if event_type in mapping:
            mapping[event_type].append(event)

    def capture_publish(self, topic: str, payload: Any) -> None:
        """Capture production broadcasts as diagnostics only.

        Production ``insight`` broadcasts are presentation/telemetry messages, not
        a canonical strategy decision boundary.  Older replay code classified an
        insight as STRATEGY_SIGNAL merely when its free-form text contained the
        word ``signal``.  That made strategy parity depend on logging text and
        asynchronous presentation ordering.  Keep broadcasts in the timeline,
        while canonical decision collections are populated from durable/runtime
        decision sources.
        """
        ts = None
        if isinstance(payload, dict):
            raw = payload.get("ts") or payload.get("timestamp")
            if isinstance(raw, str):
                try:
                    ts = datetime.fromisoformat(raw)
                except ValueError:
                    pass
        if ts is None:
            return
        symbol = payload.get("symbol", "") if isinstance(payload, dict) else ""
        self.record_event("TELEMETRY", ts, symbol, {"topic": topic, "payload": payload})

    def record_equity(self, timestamp: datetime, equity: float, cash: float, buying_power: float, open_positions: int) -> None:
        self.equity_curve.append({"timestamp": timestamp.isoformat(), "equity": round(float(equity), 8),
                                  "cash": round(float(cash), 8), "buying_power": round(float(buying_power), 8),
                                  "open_positions": int(open_positions)})

    @staticmethod
    def _sanitize(item: Any) -> Any:
        if isinstance(item, dict):
            # Replay/transport identities are deliberately excluded from parity.
            # candidate_id has a random UUID suffix in production and is used as
            # a persistence/queue correlation key; the economic scanner decision
            # is represented by timestamp/symbol/strategy/direction/prices/gates.
            # Never exclude decision, price, sizing, score, regime, or outcome data.
            ignored = {"replay_id", "id", "candidate_id", "order_id", "client_order_id", "broker_fill_key"}
            return {k: ReplayLedger._sanitize(v) for k, v in sorted(item.items()) if k not in ignored}
        if isinstance(item, list):
            return [ReplayLedger._sanitize(v) for v in item]
        if isinstance(item, datetime):
            return item.isoformat()
        return item

    @classmethod
    def _hash(cls, data: Any) -> str:
        return hashlib.sha256(json.dumps(cls._sanitize(data), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()

    def compute_stage_hashes(self) -> dict[str, str]:
        stages = {
            "scanner_hash": self.candidates, "strategy_hash": self.signals, "ml_hash": self.ml_decisions,
            "spawn_hash": self.spawn_decisions, "risk_hash": self.risk_decisions, "order_hash": self.orders,
            "fill_hash": self.fills, "trade_hash": self.trades, "equity_hash": self.equity_curve,
        }
        hashes = {name: self._hash(value) for name, value in stages.items()}
        hashes["overall_state_hash"] = self._hash({"ticks": self.production_ticks, "stages": hashes, "equity": self.equity_curve})
        return hashes

    def to_payload(self) -> dict[str, Any]:
        return {"production_ticks": self.production_ticks, "candidates": self.candidates, "signals": self.signals,
                "ml_decisions": self.ml_decisions, "spawn_decisions": self.spawn_decisions,
                "risk_decisions": self.risk_decisions, "orders": self.orders, "fills": self.fills,
                "trades": self.trades, "equity_curve": self.equity_curve, "timeline": self.timeline}
