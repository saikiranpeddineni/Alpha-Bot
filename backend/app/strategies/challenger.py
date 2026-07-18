from __future__ import annotations

from typing import Any
from datetime import datetime, UTC
from app.ai.prediction import TradeScoreEngine


class ChallengerConfig:
    """Configuration class for Challenger policies."""
    
    def __init__(
        self,
        policy_id: str,
        ml_threshold: float,
        score_threshold_offset: float = 0.0,
    ) -> None:
        self.policy_id = policy_id
        self.ml_threshold = ml_threshold
        self.score_threshold_offset = score_threshold_offset

    def create_custom_trade_score_engine(self, base_engine: TradeScoreEngine) -> TradeScoreEngine:
        """Creates a custom TradeScoreEngine with adjusted risk thresholds."""
        if self.score_threshold_offset == 0.0:
            return base_engine

        # Copy and update Pydantic risk config
        custom_risk = base_engine.risk_config.model_copy(
            update={
                "score_threshold_event": max(0.0, base_engine.risk_config.score_threshold_event + self.score_threshold_offset),
                "score_threshold_trend": max(0.0, base_engine.risk_config.score_threshold_trend + self.score_threshold_offset),
                "score_threshold_range": max(0.0, base_engine.risk_config.score_threshold_range + self.score_threshold_offset),
                "score_threshold_volatile": max(0.0, base_engine.risk_config.score_threshold_volatile + self.score_threshold_offset),
            }
        )
        return TradeScoreEngine(weights=base_engine.w, risk_config=custom_risk)

    def to_snapshot(self) -> dict:
        """Serializes the full policy configuration as an immutable versioned snapshot.
        This snapshot is persisted to DB before promotion so the exact tested config
        is auditable and reproducible. Prevents config drift between tested and deployed policy.
        """
        return {
            "policy_id": self.policy_id,
            "ml_threshold": self.ml_threshold,
            "score_threshold_offset": self.score_threshold_offset,
            "snapshot_version": "1.0",
            "snapshot_at": datetime.now(UTC).isoformat(),
        }


def get_active_challengers() -> list[ChallengerConfig]:
    """Returns the list of active Challenger policy configurations for walk-forward testing."""
    return [
        ChallengerConfig(
            policy_id="challenger_low_ml",
            ml_threshold=0.48,
        ),
        ChallengerConfig(
            policy_id="challenger_high_ml",
            ml_threshold=0.56,
        ),
        ChallengerConfig(
            policy_id="challenger_aggressive_score",
            ml_threshold=0.50,
            score_threshold_offset=-10.0, # lowers thresholds by 10 points
        ),
    ]
