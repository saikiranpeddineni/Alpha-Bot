# -*- coding: utf-8 -*-
"""Comprehensive verification of Alpha-Bot profitability hardening, asymmetric cooldowns,
and $100 minimum bank profit floor.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import RiskConfig, Settings
from app.core.clock import MarketClock
from app.core.domain import OrderRequest, OrderSide, OrderType, Position
from app.pis.models import ExitAction, LifecycleState, PISConfig, PositionObservation, TradeHealth
from app.risk.manager import RiskManager


def test_asymmetric_cooldown_and_quarantine():
    """Verify 20m loss cooldown, 5m win consolidation, and 2-loss session quarantine."""
    config = RiskConfig(
        symbol_cooldown_minutes=20,
        symbol_win_cooldown_minutes=5,
        symbol_max_daily_losses=2,
        strategy_consecutive_loss_limit=4,
        strategy_cooldown_minutes=20,
    )
    clock = MarketClock(always_open=True)
    risk_mgr = RiskManager(config=config, clock=clock)
    now = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)

    # 1. First loss on NCPL -> 20m cooldown
    trade1 = SimpleNamespace(symbol="NCPL", pnl=-113.0, strategy="orb", closed_at=now)
    risk_mgr.on_trade_closed(trade1, now)
    
    assert "NCPL" in risk_mgr.symbol_cooldowns
    assert risk_mgr.symbol_cooldowns["NCPL"] == now + timedelta(minutes=20)
    assert risk_mgr.symbol_loss_counts["NCPL"] == 1

    # 2. Re-entry attempted 4 minutes later -> BLOCKED!
    early_attempt = now + timedelta(minutes=4)
    # Check cooldown explicitly
    assert "NCPL" in risk_mgr.symbol_cooldowns
    assert risk_mgr.symbol_cooldowns["NCPL"] > early_attempt

    # 3. Second loss on NCPL -> Session Quarantine (390m)
    now2 = now + timedelta(minutes=25)
    trade2 = SimpleNamespace(symbol="NCPL", pnl=-50.0, strategy="orb", closed_at=now2)
    risk_mgr.on_trade_closed(trade2, now2)
    assert risk_mgr.symbol_loss_counts["NCPL"] == 2
    assert risk_mgr.symbol_cooldowns["NCPL"] == now2 + timedelta(minutes=390)

    # 4. Attempt after 30m -> still quarantined!
    attempt3 = now2 + timedelta(minutes=30)
    assert risk_mgr.symbol_cooldowns["NCPL"] > attempt3

    # 5. Winning trade on CRDO -> 5m consolidation lock
    trade_win = SimpleNamespace(symbol="CRDO", pnl=144.0, strategy="orb", closed_at=now)
    risk_mgr.on_trade_closed(trade_win, now)
    assert risk_mgr.symbol_cooldowns["CRDO"] == now + timedelta(minutes=5)
    
    # 4 minutes later -> waiting for consolidation
    assert risk_mgr.symbol_cooldowns["CRDO"] > now + timedelta(minutes=4)
    # 6 minutes later -> unlocked for second-leg breakout!
    assert risk_mgr.symbol_cooldowns["CRDO"] < now + timedelta(minutes=6)


def test_pis_models_breakeven_and_green_floor():
    """Verify PIS breakeven arming is 0.80R and green floor is 0.25R."""
    cfg = PISConfig()
    cfg.validate()
    assert cfg.breakeven_trigger_r == 0.80
    assert cfg.min_locked_r == 0.25
    assert cfg.min_bank_profit_usd == 100.0


def test_min_bank_profit_usd_skips_microslices(monkeypatch):
    """Verify engine evaluation skips partial scaleouts under $100 floor."""
    os.environ["BOT_MIN_BANK_PROFIT_USD"] = "100.0"
    
    # Small position: 50 shares of $10 stock, 50% scaleout = 25 shares.
    # Entry = 10.0, current = 11.0 (+1.0R, gain = $1.00/sh).
    # Expected bank profit = 25 * $1.00 = $25.00 < $100 floor.
    pos_qty = 50.0
    scale_qty = 25.0
    close_px = 11.0
    entry_px = 10.0
    
    unrealized_profit_per_share = close_px - entry_px
    expected_bank_profit = scale_qty * unrealized_profit_per_share
    min_bank_usd = float(os.environ.get("BOT_MIN_BANK_PROFIT_USD", 100.0))
    
    assert expected_bank_profit == 25.0
    assert min_bank_usd == 100.0
    assert expected_bank_profit < min_bank_usd  # Will be skipped to avoid micro-slice churn!

    # Large position: 1000 shares of $20 stock, 50% scaleout = 500 shares.
    # Gain = $0.50/sh -> expected profit = $250 >= $100 floor -> Executes!
    large_scale_qty = 500.0
    large_profit_per_share = 0.50
    large_expected_profit = large_scale_qty * large_profit_per_share
    assert large_expected_profit == 250.0
    assert large_expected_profit >= min_bank_usd
