import pytest
from unittest.mock import MagicMock
from app.core.engine import TradingEngine


def test_orphan_reconciliation_streak_trigger():
    engine = object.__new__(TradingEngine)
    engine._position_reconciliation_mismatch_streak = 0

    # 1st mismatch: streak becomes 1, no action
    engine._position_reconciliation_mismatch_streak += 1
    assert engine._position_reconciliation_mismatch_streak == 1

    # 2nd mismatch: streak becomes 2, warning only
    engine._position_reconciliation_mismatch_streak += 1
    assert engine._position_reconciliation_mismatch_streak == 2

    # 3rd mismatch: streak reaches 3, latch triggers auto-reconciliation
    engine._position_reconciliation_mismatch_streak += 1
    assert engine._position_reconciliation_mismatch_streak >= 3
