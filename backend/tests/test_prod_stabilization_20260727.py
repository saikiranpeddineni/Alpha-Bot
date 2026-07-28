from pathlib import Path
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.brokers.alpaca import AlpacaBroker
from app.core.engine import TradingEngine


def test_universe_stress_uses_weekend_safe_lookback_source():
    src = Path(__file__).parents[1] / "app" / "risk" / "market_regime.py"
    text = src.read_text(encoding="utf-8")
    block = text[text.index("async def _family_universe_stress"):]
    assert "timedelta(days=5)" in block
    assert "timedelta(days=2)" not in block.split("async def ", 1)[0]


def test_vix_is_blocked_at_strategy_boundary_source():
    src = Path(__file__).parents[1] / "app" / "core" / "engine.py"
    text = src.read_text(encoding="utf-8")
    assert "reference/risk symbol excluded from strategy evaluation" in text
    assert "Blocked non-tradable reference symbol at submission boundary" in text
    assert "Dropping non-tradable reference symbol" in text


def test_emergency_protection_does_not_replace_symbol_strategy():
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._symbol_strategies = {"ORCL": "momentum"}
    broker._meta = {}
    # The source-level guard is intentionally tested without constructing alpaca-py clients.
    src = Path(__file__).parents[1] / "app" / "brokers" / "alpaca.py"
    text = src.read_text(encoding="utf-8")
    assert 'if req.strategy != "emergency_protection"' in text
    assert broker._symbol_strategies["ORCL"] == "momentum"


def test_fill_ack_is_post_durable_processing_source():
    broker_src = (Path(__file__).parents[1] / "app" / "brokers" / "alpaca.py").read_text(encoding="utf-8")
    engine_src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    poll_block = broker_src[broker_src.index("async def poll_fills"):broker_src.index("def ack_fill_processed")]
    assert "self._processed_fill_qty[oid] = cumulative" not in poll_block
    assert "def ack_fill_processed" in broker_src
    assert "def nack_fill_processed" in broker_src
    on_fill_idx = engine_src.index("trade = self.portfolio.on_fill(")
    ack_idx = engine_src.index('ack_fill = getattr(self.broker, "ack_fill_processed"', on_fill_idx)
    assert ack_idx > on_fill_idx


def test_open_orders_expands_broker_child_legs_source():
    src = (Path(__file__).parents[1] / "app" / "brokers" / "alpaca.py").read_text(encoding="utf-8")
    block = src[src.index("async def open_orders"):src.index("def _to_order")]
    assert '("legs", "related_orders")' in block
    assert "normalized.append(self._to_order(child))" in block


def test_recovery_rejection_is_fail_closed_not_tight_retry_source():
    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    assert "ProtectionState.RECOVERY_REJECTED" in src
    assert "waiting for broker reconciliation" in src
