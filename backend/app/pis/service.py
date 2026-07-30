"""PIS Service Container.

Manages active PositionBrain instances across trading symbols,
provides concurrency serialization per symbol, tracks telemetry,
and supports state serialization for restart recovery.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from app.pis.models import PISConfig, PISDecision, PositionObservation
from app.pis.position_brain import PositionBrain

logger = logging.getLogger(__name__)


class PISService:
    """Service container for managing PositionBrain lifecycles and telemetry."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()
        self._lock = threading.Lock()
        self._brains: Dict[str, PositionBrain] = {}
        self._symbol_locks: Dict[str, threading.Lock] = {}
        self._telemetry: Dict[str, Any] = {
            "evaluations_total": 0,
            "duplicate_evaluations": 0,
            "rejected_observations": 0,
            "protection_updates": 0,
            "closed_brains_cleaned": 0,
            "last_eval_duration_ms": 0.0,
        }

    @property
    def brain_count(self) -> int:
        """Get active brain count."""
        with self._lock:
            return len(self._brains)

    def _get_symbol_lock(self, symbol: str) -> threading.Lock:
        """Get or create per-symbol lock for engine single-evaluation serialization (Priority 1)."""
        symbol_key = symbol.upper()
        with self._lock:
            if symbol_key not in self._symbol_locks:
                self._symbol_locks[symbol_key] = threading.Lock()
            return self._symbol_locks[symbol_key]

    def get_brain(self, symbol: str) -> Optional[PositionBrain]:
        """Get active PositionBrain for symbol, if present."""
        symbol_key = symbol.upper()
        with self._lock:
            brain = self._brains.get(symbol_key)
            if brain and brain.is_closed:
                # Lazy cleanup of closed brain
                del self._brains[symbol_key]
                self._telemetry["closed_brains_cleaned"] += 1
                return None
            return brain

    def observe(self, obs: PositionObservation) -> PISDecision:
        """Process observation for symbol through PIS, serializing per-symbol evaluations.

        Args:
            obs: PositionObservation snapshot.

        Returns:
            PISDecision.
        """
        symbol_key = obs.symbol.upper()
        symbol_lock = self._get_symbol_lock(symbol_key)

        # Priority 1: Guarantee exactly one evaluation per symbol at a time
        with symbol_lock:
            start_time = time.perf_counter()
            try:
                brain = self.get_brain(symbol_key)
                if brain is None:
                    # Initialize new brain
                    brain = PositionBrain(symbol_key, obs, self.config)
                    with self._lock:
                        self._brains[symbol_key] = brain
                    decision = brain.last_decision or brain.observe(obs)
                else:
                    decision = brain.observe(obs)

                # Telemetry updates
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                with self._lock:
                    self._telemetry["evaluations_total"] += 1
                    self._telemetry["last_eval_duration_ms"] = duration_ms
                    if decision.metadata.get("protection_updated"):
                        self._telemetry["protection_updates"] += 1

                return decision

            except Exception as e:
                with self._lock:
                    self._telemetry["rejected_observations"] += 1
                logger.error(f"PIS observation failed for {symbol_key}: {e}")
                raise

    def close_brain(self, symbol: str) -> None:
        """Close and remove PositionBrain for symbol (Priority 11)."""
        symbol_key = symbol.upper()
        symbol_lock = self._get_symbol_lock(symbol_key)

        with symbol_lock:
            with self._lock:
                brain = self._brains.pop(symbol_key, None)
                if brain:
                    brain.close()
                    self._telemetry["closed_brains_cleaned"] += 1

    def close_all(self) -> None:
        """Close and purge all active PositionBrains."""
        with self._lock:
            symbols = list(self._brains.keys())

        for sym in symbols:
            self.close_brain(sym)

    def get_telemetry(self) -> Dict[str, Any]:
        """Expose telemetry for observability (Priority 14)."""
        with self._lock:
            active_brains = len(self._brains)
            total_history = sum(b.history_length for b in self._brains.values())
            return {
                "active_brains": active_brains,
                "total_history_snapshots": total_history,
                "telemetry": dict(self._telemetry),
            }

    def serialize_all_states(self) -> Dict[str, Any]:
        """Serialize all brains state for restart recovery (Priority 15)."""
        with self._lock:
            return {
                sym: brain.serialize_state()
                for sym, brain in self._brains.items()
                if not brain.is_closed
            }
