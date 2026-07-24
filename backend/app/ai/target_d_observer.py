"""Target D Shadow Challenger Observer.
Passive, read-only observer that records hypothetical Target D ML decisions for production strategy candidates.
Strictly non-interfering: Zero authority over order execution, sizing, risk, or production signal decisions.
"""

from __future__ import annotations

import os
import json
import joblib
import pandas as pd
import numpy as np
from datetime import datetime, UTC
from typing import Any

from app.core.logging import applog
from app.ai.features import FEATURE_COLUMNS


class TargetDShadowObserver:
    """Completely passive, isolated observer for Target D Strategy-Conditioned Meta-Model."""

    def __init__(self, model_path: str | None = None, meta_path: str | None = None) -> None:
        base_dir = os.path.dirname(__file__)
        self.model_path = model_path or os.path.join(base_dir, "models", "target_d_shadow_challenger.joblib")
        self.meta_path = meta_path or os.path.join(base_dir, "models", "target_d_shadow_challenger.json")
        
        self.model = None
        self.metadata = {}
        self.generation_id = "unknown"
        self.threshold = 0.48
        self._load_artifact()

    def _load_artifact(self) -> None:
        """Loads immutable model and metadata artifacts."""
        try:
            if os.path.exists(self.model_path):
                self.model = joblib.load(self.model_path)
            if os.path.exists(self.meta_path):
                with open(self.meta_path, "r") as f:
                    self.metadata = json.load(f)
                self.generation_id = self.metadata.get("generation_id", "target_d_v1_20260528")
                self.threshold = float(self.metadata.get("threshold", 0.48))
        except Exception as exc:
            applog.error("target_d_observer", f"Failed to load Target D artifact: {exc}")

    def evaluate_passive(
        self,
        features: pd.Series | dict | None,
        master_prob: float | None,
        master_decision: str,
        meta_decision: str | None = None,
    ) -> dict[str, Any]:
        """Evaluates Target D model in a completely passive, read-only manner.
        
        Returns a telemetry dictionary containing Target D predictions and disagreement classification.
        GUARANTEE: Zero side-effects. Does not mutate inputs or alter pipeline decisions.
        """
        if self.model is None or features is None:
            return {
                "target_d_probability": None,
                "target_d_decision": "UNAVAILABLE",
                "target_d_threshold": self.threshold,
                "generation_id": self.generation_id,
                "disagreement_category": "UNAVAILABLE"
            }

        try:
            # Build feature array in EXACT order of FEATURE_COLUMNS
            if isinstance(features, pd.Series):
                x_vec = [float(features.get(c, 0.0)) for c in FEATURE_COLUMNS]
            elif isinstance(features, dict):
                x_vec = [float(features.get(c, 0.0)) for c in FEATURE_COLUMNS]
            else:
                x_vec = [0.0] * len(FEATURE_COLUMNS)

            X_df = pd.DataFrame([x_vec], columns=FEATURE_COLUMNS)
            probs = self.model.predict_proba(X_df)[0]
            
            # Probability of Class 1 (R >= +0.5R)
            target_d_prob = float(probs[1]) if len(probs) > 1 else float(probs[0])
            target_d_prob = round(target_d_prob, 4)
            
            target_d_pass = target_d_prob >= self.threshold
            target_d_decision = "PASS" if target_d_pass else "REJECT"

            master_pass = master_decision in ("PASS", "GRAY_ZONE_EVALUATION", "BASE_FALLBACK_BYPASS")
            
            # Categorize Disagreement
            if master_pass and target_d_pass:
                disagreement = "BOTH_PASS"
            elif not master_pass and not target_d_pass:
                disagreement = "BOTH_REJECT"
            elif master_pass and not target_d_pass:
                disagreement = "MASTER_ONLY"
            else:
                disagreement = "TARGET_D_ONLY"

            return {
                "target_d_probability": target_d_prob,
                "target_d_decision": target_d_decision,
                "target_d_threshold": self.threshold,
                "generation_id": self.generation_id,
                "disagreement_category": disagreement,
                "evaluated_at": datetime.now(UTC).isoformat()
            }
        except Exception as exc:
            applog.error("target_d_observer", f"Passive evaluation error: {exc}")
            return {
                "target_d_probability": None,
                "target_d_decision": "ERROR",
                "target_d_threshold": self.threshold,
                "generation_id": self.generation_id,
                "disagreement_category": "ERROR"
            }
