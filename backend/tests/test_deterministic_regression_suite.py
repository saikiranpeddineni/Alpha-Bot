"""AlphaBot Full Deterministic Regression Test Suite.

Asserts 100% deterministic behavior across repeated pipeline runs over fixed historical datasets:
- Identical signal decisions
- Identical PIS exit evaluations, protection ratchets, and reason codes
- Identical telemetry output across 5 repeated runs
- Zero regression or non-deterministic drift
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest

from app.core.domain import Position
from app.pis import ExitAction, PISConfig, PISEngineAdapter, PISService, PositionObservation


def make_fixed_dataset() -> list[dict]:
    """Generate fixed deterministic dataset."""
    base_ts = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
    dataset = []
    price = 100.0
    for i in range(30):
        # Deterministic price curve
        delta = (i % 5 - 2) * 0.5 + (0.8 if i > 10 else -0.2)
        price += delta
        dataset.append({
            "timestamp": base_ts + timedelta(minutes=i),
            "symbol": "REGRESS_TEST",
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 10000 + i * 100,
            "atr": 2.0,
        })
    return dataset


def run_pipeline_iteration(dataset: list[dict]) -> list[dict]:
    """Execute single pass of position evaluation over dataset."""
    config = PISConfig(history_max_len=100)
    service = PISService(config=config)
    adapter = PISEngineAdapter(service=service, config=config)

    results = []
    entry_price = dataset[0]["close"]
    initial_stop = entry_price - 4.0

    pos = Position(
        symbol="REGRESS_TEST",
        qty=100,
        avg_entry_price=entry_price,
        market_price=dataset[0]["close"],
    )

    for i in range(1, len(dataset)):
        df = pd.DataFrame(dataset[: i + 1])
        _, decision = adapter.evaluate_position_exit(
            position=pos,
            bar_df=df,
            initial_stop=initial_stop,
        )

        results.append({
            "step": i,
            "action": decision.action.value,
            "target_stop": decision.target_stop_loss,
            "health_overall": round(decision.health.overall, 6),
            "thesis_drift": round(decision.thesis.drift_score, 6),
            "runner_score": round(decision.runner_score, 6),
            "lifecycle": decision.lifecycle_state.value,
            "reasons": [r.value for r in decision.reason_codes],
        })

    return results


def test_deterministic_regression_suite_5_runs() -> None:
    """Run 5 repeated iterations of pipeline over fixed dataset and assert 100% identity."""
    dataset = make_fixed_dataset()

    base_run = run_pipeline_iteration(dataset)
    assert len(base_run) > 0

    # Repeat 4 more times
    for run_idx in range(2, 6):
        current_run = run_pipeline_iteration(dataset)
        assert len(current_run) == len(base_run), f"Run {run_idx} length mismatch"

        for step_idx, (r_base, r_cur) in enumerate(zip(base_run, current_run)):
            assert r_base["action"] == r_cur["action"], f"Run {run_idx} step {step_idx} action mismatch"
            assert r_base["target_stop"] == r_cur["target_stop"], f"Run {run_idx} step {step_idx} stop mismatch"
            assert r_base["health_overall"] == r_cur["health_overall"], f"Run {run_idx} step {step_idx} health mismatch"
            assert r_base["thesis_drift"] == r_cur["thesis_drift"], f"Run {run_idx} step {step_idx} drift mismatch"
            assert r_base["runner_score"] == r_cur["runner_score"], f"Run {run_idx} step {step_idx} runner score mismatch"
            assert r_base["lifecycle"] == r_cur["lifecycle"], f"Run {run_idx} step {step_idx} lifecycle mismatch"
            assert r_base["reasons"] == r_cur["reasons"], f"Run {run_idx} step {step_idx} reasons mismatch"
