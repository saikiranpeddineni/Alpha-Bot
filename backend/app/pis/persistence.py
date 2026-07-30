from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .models import LifecycleState
from .position_brain import PositionBrain


class SettingsStore(Protocol):
    def get_setting(self, key: str) -> Any: ...
    def set_setting(self, key: str, value: Any) -> None: ...


class PISStateStore:
    """Durable PositionBrain state so restarts do not erase trade memory."""

    KEY = "pis_position_state_v1"

    def __init__(self, db: SettingsStore) -> None:
        self.db = db

    def load_all(self) -> dict[str, dict[str, Any]]:
        raw = self.db.get_setting(self.KEY) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def save_brain(self, brain: PositionBrain) -> None:
        states = self.load_all()
        states[brain.symbol] = {
            "symbol": brain.symbol,
            "side": brain.side,
            "entry_price": brain.entry_price,
            "initial_stop": brain.initial_stop,
            "entry_time": brain.entry_time.isoformat(),
            "strategy": brain.strategy,
            "lifecycle": brain.lifecycle.value,
            "entry_health": brain.entry_health,
            "peak_r": brain.peak_r,
            "trough_r": brain.trough_r,
        }
        self.db.set_setting(self.KEY, states)

    def delete(self, symbol: str) -> None:
        states = self.load_all()
        states.pop(symbol.upper(), None)
        self.db.set_setting(self.KEY, states)

    def restore(self, symbol: str) -> PositionBrain | None:
        record = self.load_all().get(symbol.upper())
        if not record:
            return None
        brain = PositionBrain(
            symbol=record["symbol"],
            side=record["side"],
            entry_price=float(record["entry_price"]),
            initial_stop=float(record["initial_stop"]),
            entry_time=datetime.fromisoformat(record["entry_time"]),
            strategy=record.get("strategy", "unknown"),
        )
        brain.lifecycle = LifecycleState(record.get("lifecycle", LifecycleState.NEW.value))
        brain.entry_health = record.get("entry_health")
        brain.peak_r = float(record.get("peak_r", 0.0))
        brain.trough_r = float(record.get("trough_r", 0.0))
        return brain
