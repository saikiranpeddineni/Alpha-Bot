"""ML model calibration and diagnostic tools.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_calibration_metrics(shadow_signals: list[dict], horizon: str = "15m", label_threshold: float = 0.0010) -> dict:
    """Compute Brier Score, ECE, and decile-level precision and expectancy.

    A signal is labeled 1 if its forward outcome return at `horizon` is > label_threshold,
    else 0.
    """
    results = {
        "brier_score": None,
        "ece": None,
        "total_signals": 0,
        "deciles": [],
    }

    # Extract predicted probabilities and actual outcomes
    predictions = []
    actuals = []
    pnls_r = []

    for sig in shadow_signals:
        ml = sig.get("ml_confidence")
        if ml is None:
            continue

        # Get forward outcome at specified horizon
        outcomes = sig.get("outcomes", {})
        h_outcome = outcomes.get(horizon)
        if not h_outcome:
            continue

        fwd_ret = h_outcome.get("fwd_return", 0.0)
        realized_r = h_outcome.get("realized_r", 0.0)

        # Labels are 1 if forward return exceeds label_threshold (for long/short)
        # In our shadow ledger, direction is long/short, and fwd_return is already side-adjusted:
        # Long: (close - entry) / entry
        # Short: (entry - close) / entry
        # So fwd_return > label_threshold works for both!
        label = 1 if fwd_ret >= label_threshold else 0

        predictions.append(ml)
        actuals.append(label)
        pnls_r.append(realized_r)

    n = len(predictions)
    if n == 0:
        return results

    preds = np.array(predictions)
    acts = np.array(actuals)
    rs = np.array(pnls_r)

    # Brier Score
    brier = float(np.mean((preds - acts) ** 2))

    # ECE (Expected Calibration Error) - 10 bins
    bins = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    decile_stats = []

    for i in range(10):
        bin_lower = bins[i]
        bin_upper = bins[i + 1]
        
        # Bin mask
        if i == 9:
            in_bin = (preds >= bin_lower) & (preds <= bin_upper)
        else:
            in_bin = (preds >= bin_lower) & (preds < bin_upper)

        bin_count = int(np.sum(in_bin))
        if bin_count > 0:
            bin_preds = preds[in_bin]
            bin_acts = acts[in_bin]
            bin_rs = rs[in_bin]

            avg_conf = float(np.mean(bin_preds))
            avg_acc = float(np.mean(bin_acts))
            avg_r = float(np.mean(bin_rs))

            ece += (bin_count / n) * abs(avg_acc - avg_conf)

            decile_stats.append({
                "decile": i + 1,
                "range": f"[{bin_lower:.1f}-{bin_upper:.1f}]",
                "count": bin_count,
                "avg_confidence": round(avg_conf, 4),
                "avg_accuracy_winrate": round(avg_acc, 4),
                "avg_r_expectancy": round(avg_r, 4),
                "median_r_expectancy": round(float(np.median(bin_rs)), 4),
                "p5_r": round(float(np.percentile(bin_rs, 5)), 4),
                "p25_r": round(float(np.percentile(bin_rs, 25)), 4),
                "p75_r": round(float(np.percentile(bin_rs, 75)), 4),
                "p95_r": round(float(np.percentile(bin_rs, 95)), 4),
                "max_r": round(float(np.max(bin_rs)), 4),
            })
        else:
            decile_stats.append({
                "decile": i + 1,
                "range": f"[{bin_lower:.1f}-{bin_upper:.1f}]",
                "count": 0,
                "avg_confidence": 0.0,
                "avg_accuracy_winrate": 0.0,
                "avg_r_expectancy": 0.0,
                "median_r_expectancy": 0.0,
                "p5_r": 0.0,
                "p25_r": 0.0,
                "p75_r": 0.0,
                "p95_r": 0.0,
                "max_r": 0.0,
            })

    results.update({
        "brier_score": round(brier, 6),
        "ece": round(ece, 6),
        "total_signals": n,
        "deciles": decile_stats,
    })

    return results
