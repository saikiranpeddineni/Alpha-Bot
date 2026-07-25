from datetime import datetime, timezone

import pytest

from app.research.spawn.spawn_calibration_table import SpawnCalibrationTable


def _row(day, pnl, strategy="momentum", regime="normal", score=0.55):
    return {
        "ts": datetime(2026, 7, day, 14, 30, tzinfo=timezone.utc),
        "strategy": strategy,
        "side": "long",
        "regime": regime,
        "score": score,
        "ml_probability": 0.55,
        "forward_net_dollars": pnl,
        "realized_holding_mins": 30,
    }


def test_rejects_discovery_or_future_rows():
    table = SpawnCalibrationTable()
    with pytest.raises(ValueError, match="calibration leakage"):
        table.fit([_row(23, 100.0)])


def test_requires_pre_cutoff_evidence():
    table = SpawnCalibrationTable()
    with pytest.raises(ValueError, match="no eligible"):
        table.fit([])


def test_empirical_calibration_enriches_candidate_without_future_data():
    rows = [_row(10 + i % 10, 80.0 + i) for i in range(30)]
    table = SpawnCalibrationTable(prior_strength=10, min_samples=8).fit(rows)
    obs = {"strategy": "momentum", "side": "long", "regime": "normal", "score": 0.55, "ml_probability": 0.55}
    enriched = table.enrich(obs, min_confidence=0.25)
    assert enriched["calibration_available"] is True
    assert enriched["expectancy_confidence"] > 0
    assert enriched["calibrated_expected_net_dollars"] <= enriched["calibrated_mean_net_dollars"]
    assert enriched["expected_holding_mins"] > 0


def test_sparse_bucket_is_shrunk_and_can_fail_confidence_floor():
    rows = [_row(10 + i % 10, 50.0, strategy="orb") for i in range(25)]
    rows.append(_row(20, 500.0, strategy="momentum", regime="stress", score=0.90))
    table = SpawnCalibrationTable(prior_strength=20, min_samples=8).fit(rows)
    obs = {"strategy": "momentum", "side": "long", "regime": "stress", "score": 0.90, "ml_probability": 0.55}
    enriched = table.enrich(obs, min_confidence=0.50)
    assert enriched["calibration_available"] is False
    assert enriched["expectancy_confidence"] < 0.50


def test_unknown_bucket_falls_back_to_broad_empirical_prior_not_synthetic_profit():
    rows = [_row(10 + i % 10, 40.0 + i, strategy="orb") for i in range(30)]
    table = SpawnCalibrationTable(prior_strength=10, min_samples=8).fit(rows)
    obs = {"strategy": "unknown_new_strategy", "side": "short", "regime": "unknown", "score": 0.77}
    enriched = table.enrich(obs, min_confidence=0.0)
    assert enriched["calibration_available"] is True
    assert enriched["calibration_source_level"] == "global"
    assert enriched["calibrated_expected_net_dollars"] != 10000 * 0.015
