"""July-28 forensic-report corrections that are supported by current code.

This intentionally does NOT change ML/Meta thresholds: the report's claimed partial-window
warm-up mechanism is contradicted by pandas min_periods/pct_change semantics and the live
DataManager warmup path. We harden the invariant and repair observable shadow state instead.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ROOT / "backend/app/ai/features.py"
TESTS = ROOT / "backend/tests/test_20260729_forensic_report_regressions.py"

text = FEATURES.read_text(encoding="utf-8")
old = '''SESSION_MINUTES = 390\n\n\ndef compute_features'''
new = '''SESSION_MINUTES = 390\n# ret_15 needs 16 observations; er20 needs current + 20 prior observations.\n# Keep this explicit so live/replay callers cannot accidentally infer on a partial window.\nMIN_INFERENCE_BARS = 21\n\n\ndef compute_features'''
if old in text:
    text = text.replace(old, new, 1)
elif "MIN_INFERENCE_BARS = 21" not in text:
    raise RuntimeError("features.py: inference readiness anchor not found")

old = '''def latest_feature_row(df: pd.DataFrame) -> pd.Series | None:\n    \"\"\"Feature vector for the most recent bar, or None during warmup.\"\"\"\n    feats = compute_features(df)\n'''
new = '''def latest_feature_row(df: pd.DataFrame) -> pd.Series | None:\n    \"\"\"Feature vector for the most recent bar, or None until every lookback is causal-ready.\"\"\"\n    if len(df) < MIN_INFERENCE_BARS:\n        return None\n    feats = compute_features(df)\n'''
if old in text:
    text = text.replace(old, new, 1)
elif "if len(df) < MIN_INFERENCE_BARS:" not in text:
    raise RuntimeError("features.py: latest_feature_row anchor not found")
compile(text, str(FEATURES), "exec")
FEATURES.write_text(text, encoding="utf-8")

TESTS.write_text(r'''from __future__ import annotations

from datetime import UTC, date

import numpy as np
import pandas as pd
import pytest

from app.ai.features import FEATURE_COLUMNS, compute_features, latest_feature_row
from app.ai.gemini_ranker import GeminiShadowRanker
from app.ai.shadow_bridge import GeminiShadowBridge


def _bars(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-07-28 13:00:00+00:00", periods=n, freq="min")
    close = 100.0 + np.arange(n) * 0.1
    return pd.DataFrame({"open": close, "high": close + .05, "low": close - .05,
                         "close": close, "volume": 1000.0 + np.arange(n)}, index=idx)


def test_partial_windows_are_nan_not_shortened_returns():
    feats = compute_features(_bars(10))
    assert pd.isna(feats["ret_15"].iloc[-1])
    assert pd.isna(feats["er20"].iloc[-1])
    assert pd.isna(feats["vol_z"].iloc[-1])
    assert pd.isna(feats["bb_pos"].iloc[-1])


def test_latest_feature_row_fails_closed_until_full_lookback():
    assert latest_feature_row(_bars(20)) is None
    row = latest_feature_row(_bars(60))
    assert row is not None
    assert list(row.index) == FEATURE_COLUMNS
    assert np.isfinite(row.to_numpy(dtype=float)).all()


@pytest.mark.asyncio
async def test_gemini_no_key_is_observable_without_transport_call():
    calls = 0
    async def transport(_payload):
        nonlocal calls
        calls += 1
        return {"rankings": []}
    observations = []
    ranker = GeminiShadowRanker("", transport=transport)
    bridge = GeminiShadowBridge(ranker, observation_sink=observations.append)
    bridge._enqueue([{"symbol": "SPY", "action": "enter_long", "strategy": "momentum"}], date(2026, 7, 28))
    assert calls == 0
    assert observations
    telemetry = observations[-1]["telemetry"]
    assert telemetry["calls_today"] == 0
    assert telemetry["last_outcome"] == "SKIPPED_NO_API_KEY"
    assert observations[-1]["bridge"]["skipped_no_key"] == 1
''', encoding="utf-8")
compile(TESTS.read_text(encoding="utf-8"), str(TESTS), "exec")
print("Applied forensic-report correctness guards and regression tests; syntax compile passed.")
