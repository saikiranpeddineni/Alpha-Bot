from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.pis.authority import ExitAuthority, ExitAuthorityViolation, assert_close_authorized
from app.pis.engine_adapter import PISEngineAdapter


def test_legacy_discretionary_close_fails_closed():
    with pytest.raises(ExitAuthorityViolation):
        assert_close_authorized(authority=ExitAuthority.PIS, pis_requests_exit=False)


def test_confirmed_pis_exit_is_authorized():
    assert_close_authorized(authority=ExitAuthority.PIS, pis_requests_exit=True)


def test_hard_risk_remains_authoritative():
    assert_close_authorized(authority=ExitAuthority.HARD_RISK)


def test_adapter_has_no_entry_or_sizing_authority():
    adapter = PISEngineAdapter()
    assert not hasattr(adapter, "enter")
    assert not hasattr(adapter, "size_position")
    assert not hasattr(adapter, "submit_entry")


def test_adapter_reconstructs_position_brain_from_open_position():
    adapter = PISEngineAdapter()
    position = SimpleNamespace(
        qty=10,
        avg_entry_price=100.0,
        stop_loss=98.0,
        strategy="momentum",
        entry_time=datetime.now(timezone.utc),
    )
    idx = pd.date_range("2026-07-30 13:30", periods=35, freq="min", tz="UTC")
    close = [100 + i * 0.05 for i in range(35)]
    df = pd.DataFrame(
        {
            "open": close,
            "high": [x + 0.10 for x in close],
            "low": [x - 0.10 for x in close],
            "close": close,
            "volume": [1000 + i * 5 for i in range(35)],
            "atr14": [0.5] * 35,
            "ema9": [x - 0.05 for x in close],
            "ema21": [x - 0.15 for x in close],
            "vwap": [x - 0.08 for x in close],
        },
        index=idx,
    )
    result = adapter.evaluate(
        symbol="AAA",
        position=position,
        df=df,
        now=datetime.now(timezone.utc),
        relative_strength=0.5,
        market_context=75.0,
    )
    assert adapter.pis.get("AAA") is not None
    assert result.intent.suppress_legacy_exit is True
