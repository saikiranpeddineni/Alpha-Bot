import pytest
import pandas as pd
import numpy as np

from app.core.canonical_evidence import CanonicalEvidence, CanonicalOutcomeResolver
from app.strategies.memory import _canonical_realized_r


def test_canonical_r_breakeven_vs_none():
    """Test #220: realized_r = 0.0 is valid 0R breakeven trade, whereas None is missing R evidence."""
    # 0.0 PnL breakeven
    r_breakeven = CanonicalOutcomeResolver.compute_canonical_r(pnl=0.0, entry=100.0, stop=98.0)
    assert r_breakeven == 0.0

    # Missing PnL
    r_none = CanonicalOutcomeResolver.compute_canonical_r(pnl=None, entry=100.0, stop=98.0)
    assert r_none is None

    # NaN PnL
    r_nan = CanonicalOutcomeResolver.compute_canonical_r(pnl=np.nan, entry=100.0, stop=98.0)
    assert r_nan is None


def test_canonical_resolver_resolve_record():
    """Test #218, #219, #220: Resolving record into CanonicalEvidence dataclass."""
    rec_valid_0r = {
        "replayed": True,
        "replay_failed": False,
        "simulated_pnl": 0.0,
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "regime": "RANGE",
        "regime_source": "VIX"
    }
    ev1 = CanonicalOutcomeResolver.resolve(rec_valid_0r)
    assert ev1.valid is True
    assert ev1.realized_r == 0.0
    assert ev1.invalid_reason is None

    rec_missing_r = {
        "replayed": True,
        "replay_failed": False,
        "simulated_pnl": None,
        "entry_price": 100.0,
        "stop_loss": None,
        "regime": "RANGE",
        "regime_source": "VIX"
    }
    ev2 = CanonicalOutcomeResolver.resolve(rec_missing_r)
    assert ev2.valid is False
    assert ev2.realized_r is None
    assert ev2.invalid_reason == "MISSING_CANONICAL_R"

    rec_failed_replay = {
        "replayed": True,
        "replay_failed": True,
        "simulated_pnl": 50.0,
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "regime": "RANGE",
        "regime_source": "VIX"
    }
    ev3 = CanonicalOutcomeResolver.resolve(rec_failed_replay)
    assert ev3.valid is False
    assert ev3.invalid_reason == "REPLAY_NOT_VALID"


def test_mixed_regime_source_population_fail_closed():
    """Test #221, #222: Mixed VIX and SPY_VOL_PROXY regime sources fail closed for promotion eligibility."""
    records = [
        {"replayed": True, "simulated_pnl": 20.0, "entry_price": 100.0, "stop_loss": 98.0, "regime": "RANGE", "regime_source": "VIX"},
        {"replayed": True, "simulated_pnl": 30.0, "entry_price": 100.0, "stop_loss": 98.0, "regime": "RANGE", "regime_source": "SPY_VOL_PROXY"},
    ]

    evidences, meta = CanonicalOutcomeResolver.filter_population(records, fail_on_mixed_sources=True)
    assert meta["error"] == "MIXED_REGIME_SOURCES"
    assert meta["valid_r_count"] == 0
    assert len(evidences) == 0

    # Explicit filter by VIX passes only VIX population
    ev_vix, meta_vix = CanonicalOutcomeResolver.filter_population(records, regime_source="VIX", fail_on_mixed_sources=True)
    assert meta_vix["error"] is None
    assert meta_vix["valid_r_count"] == 1
    assert len(ev_vix) == 1
    assert ev_vix[0].regime_source == "VIX"


def test_memory_canonical_r_returns_none_for_missing():
    """Test #238: _canonical_realized_r in memory.py returns None for missing R evidence, preserving 0.0 breakeven."""
    outcomes_breakeven = {"15m": {"realized_r": 0.0}}
    r_bk = _canonical_realized_r("rsi2_reversion", outcomes_breakeven)
    assert r_bk == 0.0

    outcomes_missing = {"15m": {"realized_r": None}}
    r_missing = _canonical_realized_r("rsi2_reversion", outcomes_missing)
    assert r_missing is None

    outcomes_empty = {}
    r_empty = _canonical_realized_r("rsi2_reversion", outcomes_empty)
    assert r_empty is None
