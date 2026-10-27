"""Unit tests for Session Betterments:
1. Symbol Max Daily Loss Lockout (MGNX repeat loss prevention).
2. Mega-Win Cooldown (PHAT re-entry giveback prevention).
3. Range-Regime Pullback Location Discipline (chop continuation pruning).
4. High-Agreement Rescue in Range Regime (unlocking +111.4 R of shadow winners).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.domain import Regime, Signal, SignalAction, TradeRecord
from app.indicators.ta import enrich
from app.risk.manager import AccountContext, RiskManager
from app.strategies.pipeline import SignalPipeline
from app.ai.prediction import MetaPrediction
from tests.conftest import MID_SESSION, make_bars


def make_ctx(equity=100_000.0) -> AccountContext:
    return AccountContext(
        equity=equity,
        cash=equity,
        positions=[],
        day_pnl=0.0,
        session_start_equity=equity,
        peak_equity=equity,
        day_trades_today=0,
        rolling_day_trades=0,
        is_paper=True,
    )


def test_symbol_max_daily_losses_quarantines_ticker(open_clock):
    cfg = RiskConfig(
        symbol_max_daily_losses=1,
        symbol_cooldown_minutes=20,
    )
    rm = RiskManager(cfg, open_clock)

    now = datetime(2026, 10, 23, 15, 36, tzinfo=UTC)
    trade_loss = TradeRecord(
        symbol="MGNX",
        side="short",
        qty=1000,
        entry_price=2.43,
        exit_price=2.48,
        entry_at=now - timedelta(minutes=5),
        exit_at=now,
        pnl=-50.0,
        pnl_pct=-2.05,
        strategy="orb",
        exit_reason="stop_loss",
    )

    rm.on_trade_closed(trade_loss)

    # 1 loss occurred. Since max_daily_losses = 1, MGNX must be quarantined for 390m
    cooldown_until = rm.symbol_cooldowns.get("MGNX")
    assert cooldown_until is not None
    assert cooldown_until >= now + timedelta(minutes=380)

    # 33 minutes later, new entry must be rejected
    eval_time = now + timedelta(minutes=33)
    sig = Signal(symbol="MGNX", action=SignalAction.ENTER_SHORT, confidence=0.75, strategy="orb", reason="unit test")
    decision = rm.is_entry_permitted(sig, now=eval_time)
    assert not decision.approved
    assert "ticker-specific cooldown active for MGNX" in decision.reason


def test_mega_win_cooldown_locks_ticker(open_clock):
    cfg = RiskConfig(
        mega_win_cooldown_r=3.0,
        mega_win_cooldown_minutes=45,
        symbol_win_cooldown_minutes=5,
    )
    rm = RiskManager(cfg, open_clock)

    now = datetime(2026, 10, 23, 16, 11, tzinfo=UTC)
    mega_win = TradeRecord(
        symbol="PHAT",
        side="short",
        qty=2671,
        entry_price=5.53,
        exit_price=5.30,
        entry_at=now - timedelta(minutes=20),
        exit_at=now,
        pnl=614.38,
        pnl_pct=4.16,
        r_multiple=4.6,
        strategy="orb",
        exit_reason="take_profit",
    )

    rm.on_trade_closed(mega_win)

    # Cooldown should be 45 minutes, not just 5 minutes
    cooldown_until = rm.symbol_cooldowns.get("PHAT")
    assert cooldown_until is not None
    assert cooldown_until >= now + timedelta(minutes=44)

    # 7 minutes later (like the actual PHAT re-entry that lost), entry should be blocked
    eval_time = now + timedelta(minutes=7)
    sig = Signal(symbol="PHAT", action=SignalAction.ENTER_SHORT, confidence=0.75, strategy="orb", reason="unit test")
    decision = rm.is_entry_permitted(sig, now=eval_time)
    assert not decision.approved
    assert "ticker-specific cooldown active for PHAT" in decision.reason


def test_pullback_location_veto_enforced_in_range_regime():
    mock_model = MagicMock()
    mock_model.score_action.return_value = 0.70
    mock_model.score_action_detailed.return_value = MagicMock(
        probability=0.70, model_source="BASE", specialist_status="ACTIVE",
        specialist_auc=0.60, specialist_min_auc=0.53, fallback_reason=None
    )

    pipeline = SignalPipeline(
        model=mock_model,
        ml_threshold=0.48,
        risk_config=RiskConfig(enable_monte_carlo_gate=False),
    )
    pipeline.trade_score_engine.risk_config.enable_opening_blackout = False
    pipeline.trade_score_engine.risk_config.enable_entry_chase_veto = False

    # Create bars where price is 5 ATR above VWAP (stretched)
    df = make_bars(50, price=20.0)
    df["vwap"] = 18.0
    df["atr14"] = 0.30
    df.loc[df.index[-1], "close"] = 20.0  # stretch = (20.0 - 18.0) / 0.30 = 6.67 ATR (> 3.0 limit)

    sig = Signal(
        symbol="STLA",
        action=SignalAction.ENTER_LONG,
        confidence=0.75,
        strategy="pullback_rearm",
        reason="unit test",
        price=20.0,
        stop_loss=19.40,
        take_profit=21.20,
    )

    # In RANGE regime: location veto MUST be enforced (no skip allowed)
    accepted_range, rejected_range = pipeline.process("STLA", df, [sig], regime=Regime.RANGE)
    assert len(accepted_range) == 0
    assert len(rejected_range) == 1
    assert "Entry veto: location exhausted" in rejected_range[0][1]

    # In TREND_UP regime: skip_location_veto is honored
    accepted_trend, rejected_trend = pipeline.process("STLA", df, [sig], regime=Regime.TREND_UP)
    # Passed location veto in TREND_UP
    assert not any("Entry veto: location exhausted" in r[1] for r in rejected_trend)


def test_range_high_agreement_rescue_unlocks_missed_winners():
    mock_model = MagicMock()
    mock_model.score_action.return_value = 0.81
    mock_model.score_action_detailed.return_value = MagicMock(
        probability=0.81, model_source="BASE", specialist_status="ACTIVE",
        specialist_auc=0.60, specialist_min_auc=0.53, fallback_reason=None
    )

    pipeline = SignalPipeline(
        model=mock_model,
        ml_threshold=0.48,
        risk_config=RiskConfig(enable_monte_carlo_gate=False),
    )
    pipeline.trade_score_engine.risk_config.enable_opening_blackout = False

    # Mock meta prediction with approval
    pipeline.meta_label_engine.predict = MagicMock(return_value=MetaPrediction(
        strategy="orb",
        prob_profitable=0.55,
        approved=True,
        threshold=0.48,
        n_training_samples=1000,
        decision="PASS",
        source="REAL_MODEL",
        reason="",
    ))

    # Mock trade score engine to return raw_score = 67.0
    pipeline.trade_score_engine.compute = MagicMock(return_value=67.0)

    df = make_bars(50, price=45.0)
    sig = Signal(
        symbol="RHI",
        action=SignalAction.ENTER_LONG,
        confidence=0.80,
        strategy="orb",
        reason="unit test",
        price=45.0,
        stop_loss=44.20,
        take_profit=46.60,
    )

    # In RANGE regime: raw 67.0 * 0.55 regime weight = 36.8 (< 65.0 threshold)
    # But because raw 67.0 >= 65.0, ml 0.81 >= 0.52, and meta PASS:
    # High-Agreement Rescue MUST rescue this trade!
    accepted, rejected = pipeline.process("RHI", df, [sig], regime=Regime.RANGE)
    assert len(accepted) == 1
    assert len(rejected) == 0
    assert accepted[0].ml_gate_decision == "HIGH_AGREEMENT_RESCUE"
    assert accepted[0].risk_multiplier == 0.65
