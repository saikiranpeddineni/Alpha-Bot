import numpy as np
import pandas as pd
import pytest
from app.ai.features import compute_features, build_training_set
from app.indicators.ta import enrich


def test_enrich_with_duplicate_timestamps():
    dates = pd.to_datetime(["2026-10-26 09:30:00", "2026-10-26 09:30:00", "2026-10-26 09:31:00"], utc=True)
    df = pd.DataFrame(
        {
            "open": [100.0, 100.5, 101.0],
            "high": [101.0, 101.5, 102.0],
            "low": [99.0, 99.5, 100.0],
            "close": [100.5, 101.0, 101.5],
            "volume": [1000, 1500, 2000],
        },
        index=dates,
    )
    enriched = enrich(df)
    assert not enriched.index.has_duplicates
    assert len(enriched) == 2


def test_build_training_set_with_duplicate_timestamps():
    # Construct 100 synthetic bars with some duplicated timestamps
    base_dates = pd.date_range("2026-10-26 09:30:00", periods=100, freq="1min", tz="UTC")
    # Duplicate some dates
    dates = list(base_dates) + list(base_dates[:10])
    dates.sort()
    df = pd.DataFrame(
        {
            "open": np.linspace(100, 110, len(dates)),
            "high": np.linspace(101, 111, len(dates)),
            "low": np.linspace(99, 109, len(dates)),
            "close": np.linspace(100.5, 110.5, len(dates)),
            "volume": np.random.randint(100, 1000, len(dates)),
        },
        index=pd.DatetimeIndex(dates),
    )
    assert df.index.has_duplicates
    X, y, w = build_training_set(df)
    assert not X.index.has_duplicates
    assert not y.index.has_duplicates
    assert not w.index.has_duplicates
    assert len(X) == len(y) == len(w)
