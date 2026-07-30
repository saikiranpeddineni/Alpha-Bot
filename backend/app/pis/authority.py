"""PIS Authority Module.

PIS evaluation authority orchestrating decision generation for position instances.
"""

from __future__ import annotations

from typing import Optional

from app.pis.exit_planner import ExitPlanner
from app.pis.models import PISConfig, PISDecision, PositionObservation
from app.pis.protection import AdaptiveProtectionPlanner
from app.pis.runner_intelligence import RunnerIntelligence
from app.pis.thesis_drift import ThesisDriftEvaluator
from app.pis.trade_health import TradeHealthEvaluator


class PISAuthority:
    """Evaluation authority combining all PIS intelligence modules."""

    def __init__(self, config: Optional[PISConfig] = None) -> None:
        self.config = config or PISConfig()
        self.health_evaluator = TradeHealthEvaluator(self.config)
        self.thesis_evaluator = ThesisDriftEvaluator(self.config)
        self.runner_intelligence = RunnerIntelligence(self.config)
        self.protection_planner = AdaptiveProtectionPlanner(self.config)
        self.exit_planner = ExitPlanner(self.config)
