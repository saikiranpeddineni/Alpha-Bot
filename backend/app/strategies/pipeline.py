"""Compatibility shell for the retired multi-gate signal pipeline.

AlphaDesk 007 no longer uses MetaLabel, TradeScore, adaptive expectancy,
thesis-memory vetoes, or parallel evidence in the production entry path.
Entry decisions live in ``core.engine_evaluation``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.domain import Signal, SignalAction


@dataclass
class _NoOpThesisMemory:
    def record_attempt(self, **kwargs: Any) -> None:
        return None

    def update_attempt_outcome(self, **kwargs: Any) -> None:
        return None


class SignalPipeline:
    """Deprecated compatibility API. Production entry decisions do not call it."""

    def __init__(self, model, ml_threshold: float = 0.50, risk_config=None, settings=None, **kwargs) -> None:
        self.model = model
        self.ml_threshold = 0.50
        self.risk_config = risk_config
        self.settings = settings
        self.soft_composite_gating = False
        self._is_event_day = False
        self._midday_active = False
        self.thesis_memory = _NoOpThesisMemory()
        # Startup compatibility for the legacy engine constructor. These are
        # intentionally absent rather than instantiated research engines.
        self.meta_label_engine = None
        self.trade_score_engine = None

    def set_event_day(self, is_event: bool) -> None:
        self._is_event_day = bool(is_event)

    def set_midday_active(self, is_midday: bool) -> None:
        self._midday_active = bool(is_midday)

    def process(self, symbol, df, signals, regime, features=None, sector_strength=None, adaptive_ml_thresholds=None):
        accepted = [s for s in signals if s.action == SignalAction.EXIT]
        accepted.extend(s for s in signals if s.action != SignalAction.EXIT)
        return accepted, []
