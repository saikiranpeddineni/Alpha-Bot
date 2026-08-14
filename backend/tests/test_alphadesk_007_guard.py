from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.config import RiskConfig
from app.core.alphadesk_007_guard import (
    ENTRY_HARD_ML_CUTOFF,
    ENTRY_ML_THRESHOLD,
    HARD_RISK_CAP_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_OPEN_POSITIONS,
    TARGET_RISK_PCT,
    _minimum_stop_distance,
)
from app.core.domain import Signal, SignalAction
from app.risk.manager import AccountContext, RiskManager
from app.strategies.base import PRODUCTION_STRATEGIES, REGISTRY, create_strategies


class _Clock:
    def in_entry_window(self, _minutes, _now=None):
        return True

    def in_flatten_window(self, _minutes, _now=None):
        return False

    def session_date(self, now=None):
        return (now or datetime.now(timezone.utc)).date()


def _bars(price: float = 100.0, atr: float = 2.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close": [price] * 120,
            "volume": [20_000] * 120,
            "atr14": [atr] * 120,
        }
    )


def test_guard_constants_are_conservative_but_trade_enabled():
    assert TARGET_RISK_PCT == 0.25
    assert HARD_RISK_CAP_PCT == 0.40
    assert MAX_OPEN_POSITIONS == 5
    assert MAX_DAILY_LOSS_PCT == 1.50
    assert ENTRY_ML_THRESHOLD == 0.50
    assert ENTRY_HARD_ML_CUTOFF == 0.46
    assert _minimum_stop_distance(100.0, 2.0) == 1.0


def test_runtime_risk_configuration_is_clamped():
    cfg = RiskConfig(
        risk_per_trade_pct=1.0,
        max_open_positions=15,
        max_daily_loss_pct=4.0,
    )
    manager = RiskManager(cfg, _Clock())
    assert manager.config.risk_per_trade_pct <= TARGET_RISK_PCT
    assert manager.config.max_open_positions <= MAX_OPEN_POSITIONS
    assert manager.config.max_daily_loss_pct <= MAX_DAILY_LOSS_PCT
    assert manager.config.ml_confidence_threshold <= ENTRY_ML_THRESHOLD
    assert manager.config.ml_hard_reject_threshold <= ENTRY_HARD_ML_CUTOFF


def test_production_strategy_set_is_small_and_explicit():
    assert PRODUCTION_STRATEGIES == {
        "rsi2_reversion",
        "orb",
        "vwap_reversion",
        "trend_pullback",
    }
    strategies = create_strategies({name: {"enabled": True, "params": {}} for name in REGISTRY})
    enabled = {s.name for s in strategies if s.enabled}
    assert enabled == PRODUCTION_STRATEGIES


def test_tiny_stop_is_rejected_before_sizing():
    cfg = RiskConfig(risk_per_trade_pct=1.0)
    manager = RiskManager(cfg, _Clock())
    signal = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.9,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=99.95,
    )
    ctx = AccountContext(
        equity=100_000.0,
        cash=100_000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100_000.0,
        peak_equity=100_000.0,
        day_trades_today=0,
        is_paper=True,
    )
    decision = manager.evaluate_entry(signal, ctx, _bars(), now=datetime.now(timezone.utc))
    assert decision.approved is False
    assert "stop" in decision.reason.lower()


def test_final_size_is_bounded_by_007_risk_cap():
    cfg = RiskConfig(risk_per_trade_pct=1.0)
    manager = RiskManager(cfg, _Clock())
    signal = Signal(
        symbol="TEST",
        action=SignalAction.ENTER_LONG,
        confidence=0.9,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=95.0,
    )
    ctx = AccountContext(
        equity=100_000.0,
        cash=100_000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100_000.0,
        peak_equity=100_000.0,
        day_trades_today=0,
        is_paper=True,
    )
    decision = manager.evaluate_entry(signal, ctx, _bars(), now=datetime.now(timezone.utc))
    assert decision.approved is True
    assert decision.order is not None
    assert decision.order.qty <= 50
    assert decision.order.metadata.get("alphadesk_007_guard") is True
    assert float(decision.order.metadata["planned_risk_pct"]) <= 0.25
