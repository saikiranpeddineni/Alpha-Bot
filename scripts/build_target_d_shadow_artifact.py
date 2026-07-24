import os
import sys
import json
import joblib
import pandas as pd
import numpy as np
from datetime import datetime, UTC
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, os.path.abspath('backend'))
from app.ai.features import FEATURE_COLUMNS

print("=== BUILDING TARGET D SHADOW CHALLENGER IMMUTABLE MODEL ARTIFACT ===")

events_path = 'scratch/ml_alignment_research/strategy_candidate_events.parquet'
if not os.path.exists(events_path):
    print(f"Error: {events_path} missing.")
    sys.exit(1)

df_events = pd.read_parquet(events_path).sort_values('datetime').reset_index(drop=True)

# Strict Training Cutoff Enforcement: <= 2026-05-28 16:30:00 UTC
cutoff_dt = pd.to_datetime('2026-05-28 16:30:00+00:00')
train_events = df_events[df_events['datetime'] <= cutoff_dt].copy()

print(f"Total Candidate Events : {len(df_events)}")
print(f"Train Cutoff           : <= {cutoff_dt}")
print(f"Train Candidate Events : {len(train_events)}")

# Verify Date Boundary
assert train_events['datetime'].max() <= cutoff_dt, "Leakage Error: Train event exceeds cutoff!"

X_train_list = [[f.get(c, 0.0) for c in FEATURE_COLUMNS] for f in train_events['features']]
X_train = pd.DataFrame(X_train_list, columns=FEATURE_COLUMNS)

# Target D Training Target: realized_r >= +0.5R
y_train = (train_events['realized_r'] >= 0.5).astype(int)

print(f"Target D Positive Class Rate (R >= 0.5R): {y_train.mean()*100:.2f}% ({y_train.sum()}/{len(y_train)})")

clf = HistGradientBoostingClassifier(random_state=42, max_iter=100)
clf.fit(X_train, y_train)

# Persist Immutable Model Artifact
model_dir = 'backend/app/ai/models'
os.makedirs(model_dir, exist_ok=True)

model_path = os.path.join(model_dir, 'target_d_shadow_challenger.joblib')
meta_path = os.path.join(model_dir, 'target_d_shadow_challenger.json')

joblib.dump(clf, model_path)

metadata = {
    "generation_id": "target_d_v1_20260528",
    "model_type": "HistGradientBoostingClassifier",
    "training_target": "realized_r >= +0.5R",
    "trained_at_cutoff": cutoff_dt.isoformat(),
    "trained_at": datetime.now(UTC).isoformat(),
    "sample_count": len(train_events),
    "features": FEATURE_COLUMNS,
    "threshold": 0.48,
    "classes": [0, 1]
}

with open(meta_path, 'w') as f:
    json.dump(metadata, f, indent=2)

print(f"\nSuccessfully saved model to: {model_path}")
print(f"Successfully saved meta to : {meta_path}")
