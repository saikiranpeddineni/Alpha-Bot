import sys
sys.path.append("backend")

from app.strategies.pipeline import SignalPipeline
from app.ai.prediction import MetaPrediction, MetaLabelEngine
from app.core.domain import Signal, SignalAction
import pandas as pd
from datetime import datetime, timezone
import json

class DummyTradeScoreEngine:
    def __init__(self):
        class Config:
            ml_hard_reject_threshold = 0.48
            midday_score_boost = 15.0
        self.risk_config = Config()
        settings.trade_score_weights = lambda *args, **kwargs: {"win_rate": 0.35, "profit_factor": 0.25, "expectancy": 0.40}
    def compute(self, **kwargs):
        return 80.0
    def approve(self, *args, **kwargs):
        return True, 55.0

class DummyMetaLabelEngine:
    def predict(self, strategy, features, side, adaptive_threshold=None):
        prob = 0.4023
        threshold = adaptive_threshold if adaptive_threshold is not None else 0.55
        
        # Copied exact logic from prediction.py
        vol_z = float(features.get("vol_z", 0.0))
        if vol_z > 3.0 and prob < threshold and prob >= 0.45:
            threshold = 0.45
            
        decision = "PASS" if prob >= threshold else "REJECT"
        approved = prob >= threshold
        
        return MetaPrediction(
            strategy=strategy,
            prob_profitable=prob,
            approved=approved,
            threshold=threshold,
            n_training_samples=100,
            source="REAL_MODEL",
            decision=decision,
            reason="" if approved else f"prob_below_threshold"
        )

# Mock model
class DummyModel:
    def score_action(self, *args, **kwargs):
        return 0.8079
    def score_action_detailed(self, *args, **kwargs):
        class Res:
            probability = 0.8079
            model_source = "BASE"
            specialist_status = "NO_ARTIFACT"
            specialist_auc = None
            specialist_min_auc = 0.53
            fallback_reason = None
        return Res()

risk_config = type('RiskConfig', (), {'min_stop_pct': 0.5, 'midday_score_boost': 15.0})()
settings = type('Settings', (), {'trade_score_weights': lambda *args, **kwargs: (0.3, 0.2, 0.1, 0.1, 0.1, 0.1)})()

pipeline = SignalPipeline(DummyModel(), 0.55, risk_config=risk_config, settings=settings)
pipeline.meta_label_engine = DummyMetaLabelEngine()
pipeline.trade_score_engine = DummyTradeScoreEngine()

sig = Signal(
    symbol="BE",
    action=SignalAction.ENTER_SHORT,
    confidence=0.8,
    strategy="orb",
    reason="test",
    price=219.4451,
    ts=datetime.now(timezone.utc),
    candidate_id="BE_ORB_20260807_163900_C51CB2"
)

accepted, rejected = pipeline.process(
    symbol="BE",
    df=None,
    signals=[sig],
    regime="NORMAL",
    features=pd.Series({"vol_z": 0.0}),
    sector_strength=1.0,
    adaptive_ml_thresholds=None
)

print(f"Accepted: {len(accepted)}")
print(f"Rejected: {len(rejected)}")
for s, r in rejected:
    print(f"Rejected reason: {r}")
