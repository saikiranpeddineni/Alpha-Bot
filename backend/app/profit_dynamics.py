"""Runtime-derived profit adaptation helpers.

These helpers deliberately derive adjustments from the bot's existing runtime
configuration and observed contextual evidence. They do not introduce fixed
strategy-specific thresholds into the execution path.
"""

from __future__ import annotations

import math
from typing import Any


def _runtime_risk_config() -> Any:
    """Load the current persisted risk configuration when available."""
    try:
        from app.config import RiskConfig
        from app.db.database import Database

        base = RiskConfig().model_dump()
        db = Database.active()
        stored = db.get_setting("risk_config") if db else None
        if isinstance(stored, dict):
            base.update(stored)
        return RiskConfig(**base)
    except Exception:
        return None


def adaptive_ml_threshold(base_threshold: float, exp_info: dict | None, sctx: Any = None) -> float:
    """Derive a confidence threshold from observed context without fixed edges.

    Local context is only allowed to influence the threshold after the configured
    minimum observation count. The adjustment is proportional to the observed
    contextual edge and automatically shrinks toward the supplied base threshold
    when evidence is weak.
    """
    cfg = _runtime_risk_config()
    base = float(base_threshold)
    if not math.isfinite(base):
        return base_threshold
    base = min(max(base, 0.0), 1.0)

    if cfg is None or not exp_info:
        return base

    sample_count = int(exp_info.get("trades_count") or 0)
    min_samples = int(getattr(cfg, "promotion_min_valid_observations"))
    if sample_count < min_samples:
        return base

    expectancy = float(exp_info.get("shrunk_expectancy") or 0.0)
    if not math.isfinite(expectancy):
        return base

    evidence_confidence = sample_count / max(sample_count + min_samples, 1)
    edge_scale = max(abs(expectancy), 1.0)
    edge_signal = math.tanh(expectancy / edge_scale)
    adaptive_weight = float(getattr(cfg, "adaptive_model_weight"))
    neutral = 0.5
    available_room = min(base, 1.0 - base)
    adjustment = available_room * adaptive_weight * evidence_confidence * edge_signal
    threshold = base - adjustment

    # Opening behaviour is intentionally left to the existing market-regime and
    # data-quality gates. A fixed time-of-day confidence penalty is not adaptive.
    return min(max(threshold, 0.0), 1.0)


def adaptive_signal_score(base_score: float, exp_info: dict | None) -> float:
    """Keep the champion score immutable; contextual memory remains research-only."""
    return float(base_score)
