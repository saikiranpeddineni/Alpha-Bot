"""Target D passive shadow challenger. Never has production execution authority."""
from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from typing import Any

import joblib
import numpy as np
import pandas as pd

from app.ai.features import FEATURE_COLUMNS
from app.core.logging import applog


class TargetDShadowObserver:
    def __init__(self, model_path: str | None = None, meta_path: str | None = None) -> None:
        base_dir = os.path.dirname(__file__)
        self.model_path = model_path or os.path.join(base_dir, "models", "target_d_shadow_challenger.joblib")
        self.meta_path = meta_path or os.path.join(base_dir, "models", "target_d_shadow_challenger.json")
        self.model = None
        self.metadata: dict[str, Any] = {}
        self.generation_id = "unknown"
        self.threshold = 0.48
        self.unavailable_reason: str | None = None
        self._load_artifact()

    def _load_artifact(self) -> None:
        try:
            if not os.path.exists(self.model_path):
                self.unavailable_reason = "MODEL_ARTIFACT_MISSING"
                return
            self.model = joblib.load(self.model_path)
            if not hasattr(self.model, "predict_proba"):
                raise ValueError("Target D artifact does not expose predict_proba")
            if os.path.exists(self.meta_path):
                with open(self.meta_path, "r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                if not isinstance(metadata, dict):
                    raise ValueError("Target D metadata must be an object")
                self.metadata = metadata
                self.generation_id = str(metadata.get("generation_id", "target_d_v1_20260528"))
                threshold = float(metadata.get("threshold", 0.48))
                if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
                    raise ValueError("Target D threshold must be finite and within [0,1]")
                self.threshold = threshold
            self.unavailable_reason = None
        except Exception as exc:
            applog.error("target_d_observer", f"Failed to load Target D artifact: {exc}")
            self.model = None
            self.unavailable_reason = f"ARTIFACT_LOAD_FAILED:{type(exc).__name__}"

    def _unavailable(self, reason: str | None = None) -> dict[str, Any]:
        return {
            "target_d_probability": None,
            "target_d_decision": "UNAVAILABLE",
            "target_d_threshold": self.threshold,
            "generation_id": self.generation_id,
            "disagreement_category": "UNAVAILABLE",
            "unavailable_reason": reason or self.unavailable_reason or "EVIDENCE_UNAVAILABLE",
        }

    def evaluate_passive(self, features: pd.Series | dict | None, master_prob: float | None,
                         master_decision: str, meta_decision: str | None = None) -> dict[str, Any]:
        if self.model is None:
            return self._unavailable()
        if features is None:
            return self._unavailable("FEATURES_MISSING")
        try:
            x_vec: list[float] = []
            for col in FEATURE_COLUMNS:
                val = features.get(col) if isinstance(features, (pd.Series, dict)) else None
                if val is None:
                    return self._unavailable(f"FEATURE_MISSING:{col}")
                try:
                    numeric = float(val)
                except (TypeError, ValueError):
                    return self._unavailable(f"FEATURE_INVALID:{col}")
                if not np.isfinite(numeric):
                    return self._unavailable(f"FEATURE_NONFINITE:{col}")
                x_vec.append(numeric)

            X_df = pd.DataFrame([x_vec], columns=FEATURE_COLUMNS)
            probs = np.asarray(self.model.predict_proba(X_df)[0], dtype=float)
            if probs.ndim != 1 or probs.size == 0 or not np.all(np.isfinite(probs)):
                return self._unavailable("INVALID_PROBABILITY_OUTPUT")

            classes = list(getattr(self.model, "classes_", []))
            if 1 in classes:
                positive_idx = classes.index(1)
            elif True in classes:
                positive_idx = classes.index(True)
            elif probs.size == 2:
                positive_idx = 1
            else:
                return self._unavailable("POSITIVE_CLASS_UNRESOLVED")
            if positive_idx >= probs.size:
                return self._unavailable("POSITIVE_CLASS_INDEX_INVALID")

            target_d_prob = round(float(probs[positive_idx]), 4)
            if not 0.0 <= target_d_prob <= 1.0:
                return self._unavailable("PROBABILITY_OUT_OF_RANGE")
            target_d_pass = target_d_prob >= self.threshold
            target_d_decision = "PASS" if target_d_pass else "REJECT"
            master_pass = master_decision in ("PASS", "GRAY_ZONE_EVALUATION", "BASE_FALLBACK_BYPASS", "TEMPORAL_BYPASS")
            if master_pass and target_d_pass:
                disagreement = "BOTH_PASS"
            elif not master_pass and not target_d_pass:
                disagreement = "BOTH_REJECT"
            elif master_pass:
                disagreement = "MASTER_ONLY"
            else:
                disagreement = "TARGET_D_ONLY"
            return {
                "target_d_probability": target_d_prob,
                "target_d_decision": target_d_decision,
                "target_d_threshold": self.threshold,
                "generation_id": self.generation_id,
                "disagreement_category": disagreement,
                "master_probability": master_prob,
                "master_decision": master_decision,
                "meta_decision": meta_decision,
                "evaluated_at": datetime.now(UTC).isoformat(),
                "unavailable_reason": None,
            }
        except Exception as exc:
            applog.error("target_d_observer", f"Passive evaluation error: {exc}")
            return self._unavailable(f"PREDICTION_FAILED:{type(exc).__name__}")
