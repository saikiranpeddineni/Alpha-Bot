import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV


def test_ml_calibration_wrapper():
    # Generate synthetic dataset where raw probabilities might cluster
    np.random.seed(42)
    n = 300
    X = pd.DataFrame({
        "feat1": np.random.randn(n),
        "feat2": np.random.randn(n),
    })
    # Target strongly correlated with feat1
    y = (X["feat1"] + 0.5 * np.random.randn(n) > 0).astype(int)

    base = HistGradientBoostingClassifier(max_iter=50, random_state=42)
    calibrated = CalibratedClassifierCV(estimator=base, method="sigmoid", cv=3)
    calibrated.fit(X, y)

    # Predictions on extreme inputs should spread well outside [0.45, 0.55]
    extreme_X = pd.DataFrame({
        "feat1": [3.0, -3.0],
        "feat2": [0.0, 0.0],
    })
    probs = calibrated.predict_proba(extreme_X)[:, 1]

    # Extreme positive should be high, extreme negative should be low
    assert probs[0] > 0.65, f"Expected high probability for positive extreme, got {probs[0]}"
    assert probs[1] < 0.35, f"Expected low probability for negative extreme, got {probs[1]}"
