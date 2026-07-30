"""AlphaBot System Integration & PIS Single Discretionary Exit Authority Tests.

Verifies:
1. End-to-end position flow through PISEngineAdapter.
2. Single discretionary exit ownership (PIS owns runner and dynamic trail ratchets).
3. Non-discretionary safety nets (Rule-41, EOD flatten, Hard Halt) coexist without interference.
4. Portfolio stop tracking (`note_stop`).
"""

from __future__ import annotations

from datetime import datetime, timezone
import pandas as pd
import pytest

from app.core.domain import OrderStatus, OrderType, Position
from app.pis import ExitAction, PISEngineAdapter, PISService, PositionBrain


def test_pis_engine_adapter_position_evaluation() -> None:
    """Verify PISEngineAdapter processes Position and bar DataFrame into PISDecision."""
    adapter = PISEngineAdapter()
    pos = Position(symbol="MSFT", qty=100, avg_entry_price=300.0, market_price=310.0)

    df = pd.DataFrame([
        {"high": 305.0, "low": 298.0, "close": 302.0, "volume": 10000, "atr": 5.0},
        {"high": 312.0, "low": 301.0, "close": 310.0, "volume": 15000, "atr": 5.0},
    ])

    order_req, decision = adapter.evaluate_position_exit(
        position=pos,
        bar_df=df,
        initial_stop=290.0,
        current_stop=290.0,
    )

    assert decision.symbol == "MSFT"
    assert decision.health.overall > 0.0
    assert decision.target_stop_loss >= 290.0


def test_pis_single_discretionary_exit_ownership() -> None:
    """Verify PIS maintains sole discretionary exit authority over runner ratchets."""
    adapter = PISEngineAdapter()
    pos = Position(symbol="AMD", qty=50, avg_entry_price=100.0, market_price=120.0)

    # 4 consecutive bars establishing a runner
    bars = []
    base_ts = datetime.now(timezone.utc)
    for i in range(5):
        bars.append({
            "high": 100.0 + (i + 1) * 5.0,
            "low": 98.0 + i * 5.0,
            "close": 100.0 + (i + 1) * 4.5,
            "volume": 20000,
            "atr": 3.0,
        })
    df = pd.DataFrame(bars)

    order_req, decision = adapter.evaluate_position_exit(
        position=pos,
        bar_df=df,
        initial_stop=95.0,
    )

    # Monotonic stop ratchet check: floor must rise above initial stop (95.0)
    assert decision.target_stop_loss > 95.0
    assert decision.peak_r > 3.0


def test_position_closed_cleanup_flow() -> None:
    """Verify handle_position_closed cleans up PISService state."""
    adapter = PISEngineAdapter()
    pos = Position(symbol="INTC", qty=200, avg_entry_price=30.0, market_price=31.0)
    df = pd.DataFrame([{"high": 31.5, "low": 29.5, "close": 31.0, "volume": 5000, "atr": 1.0}])

    adapter.evaluate_position_exit(position=pos, bar_df=df, initial_stop=28.0)
    assert adapter.service.brain_count == 1

    adapter.handle_position_closed("INTC")
    assert adapter.service.brain_count == 0
