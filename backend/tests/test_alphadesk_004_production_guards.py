from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from app.core.domain import Regime, SignalAction
from app.config import RiskConfig, Settings
from app.risk.manager import RiskManager
from app.strategies.entry_intelligence import assess_entry_context, assess_thesis_continuity
from app.strategies.runtime_integrity import install_runtime_integrity


class _Clock:
    def in_entry_window(self, _minutes, _now=None):
        return True

    def session_date(self, now=None):
        return (now or datetime.now(UTC)).date()


def _signal(action, **kwargs):
    base = dict(
        action=action,
        side="long" if action == SignalAction.ENTER_LONG else "short",
        strategy="momentum",
        symbol="TEST",
        mover_bias=None,
        mover_change_pct=0.0,
        rs_score=None,
        news_context_available=False,
        news_direction="neutral",
        news_sentiment=0.0,
        news_confidence=0.0,
        news_catalyst=None,
        news_age_seconds=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _bars(closes, ema9=101.0, ema20=100.0):
    n = len(closes)
    return pd.DataFrame(
        {
            "close": closes,
            "high": [x + 0.2 for x in closes],
            "low": [x - 0.2 for x in closes],
            "atr14": [1.0] * n,
            "ema9": [ema9] * n,
            "ema20": [ema20] * n,
        }
    )


def test_champion_entry_context_requires_multiple_independent_conflicts():
    sig = _signal(SignalAction.ENTER_SHORT, mover_bias="gainer", rs_score=0.60)
    closes = [100.0 + i * 0.2 for i in range(25)]
    result = assess_entry_context(sig, _bars(closes), Regime.TREND_UP)
    assert result.allowed is False
    assert result.reasons


def test_countertrend_reversal_is_not_blanket_blocked():
    sig = _signal(SignalAction.ENTER_SHORT)
    closes = [104.0, 103.8, 103.6, 103.4, 103.2, 103.0, 103.2, 102.8, 102.4, 102.0]
    closes += [101.6, 101.2, 100.8, 100.4, 100.0, 99.6, 99.2, 98.8, 98.4, 98.0]
    result = assess_entry_context(sig, _bars(closes, ema9=101.0, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True


def test_fresh_material_news_is_contradiction_when_price_confirms_it():
    sig = _signal(
        SignalAction.ENTER_LONG,
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.9,
        news_catalyst="financing",
        news_age_seconds=120,
    )
    closes = [100.0 + i * 0.15 for i in range(20)] + [103.0, 102.8, 102.6, 102.4, 102.2]
    result = assess_entry_context(sig, _bars(closes, ema9=101.5, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True
    assert any("news_context_conflict" in reason for reason in result.reasons)


def test_adverse_news_is_not_a_contradiction_when_price_absorbs_it():
    sig = _signal(
        SignalAction.ENTER_LONG,
        news_context_available=True,
        news_direction="bearish",
        news_confidence=0.9,
        news_catalyst="financing",
        news_age_seconds=120,
    )
    closes = [100.0 + i * 0.15 for i in range(25)]
    result = assess_entry_context(sig, _bars(closes, ema9=101.5, ema20=100.0), Regime.TREND_UP)
    assert result.allowed is True
    assert not any("news_context_conflict" in reason for reason in result.reasons)
    assert result.evidence["news_reaction_atr"] is not None


def test_choppy_momentum_is_softly_flagged_not_globally_blocked():
    sig = _signal(SignalAction.ENTER_LONG)
    closes = [100.0 + (0.02 if i % 2 == 0 else -0.02) for i in range(25)]
    result = assess_entry_context(sig, _bars(closes, ema9=100.0, ema20=100.0), Regime.RANGE)
    assert result.allowed is True
    assert any("macro_context_conflict" in reason for reason in result.reasons)


class _ShadowDb:
    def get_shadow_signals(self, limit=500):
        return [
            {
                "symbol": "TEST",
                "direction": "long",
                "timestamp": datetime.now(UTC).isoformat(),
                "regime": "trend_up",
                "mover_bias": "neutral",
                "outcomes": {"60m": {"realized_r": -1.0}},
            }
        ]


def test_new_material_catalyst_is_recorded_without_restrictive_thesis_veto():
    sig = _signal(
        SignalAction.ENTER_LONG,
        news_context_available=True,
        news_direction="bullish",
        news_confidence=0.85,
        news_catalyst="earnings",
        news_age_seconds=120,
    )
    result = assess_thesis_continuity(_ShadowDb(), sig, datetime.now(UTC))
    assert result.allowed is True
    assert any("thesis_history_observation" in reason for reason in result.reasons)
    assert result.evidence["previous_realized_r"] == -1.0


def test_restart_does_not_clear_restored_cooldowns():
    install_runtime_integrity()
    config = RiskConfig()
    risk = RiskManager(config, _Clock())
    now = datetime.now(UTC)
    risk.symbol_cooldowns["PANW"] = now + timedelta(minutes=20)
    risk.strategy_cooldowns["momentum"] = now + timedelta(minutes=15)
    risk.roll_session(date.today())
    assert "PANW" in risk.symbol_cooldowns
    assert "momentum" in risk.strategy_cooldowns


def test_restart_preserves_restored_daily_halt_state():
    install_runtime_integrity()
    risk = RiskManager(RiskConfig(), _Clock())
    risk.daily_halted = True
    risk.daily_halt_date = date.today()
    risk.reconstruct_cooldowns([], datetime.now(UTC))
    assert risk.daily_halted is True
    assert risk.daily_halt_date == date.today()


def test_pipeline_intelligence_thresholds_are_canonical_and_configurable():
    settings = Settings()
    risk = settings.risk_config()
    assert risk.ml_hard_reject_threshold == 0.48
    assert risk.ml_gray_zone_margin == 0.04
    assert risk.ml_hard_reject_threshold == RiskConfig().ml_hard_reject_threshold
    assert risk.ml_gray_zone_margin == RiskConfig().ml_gray_zone_margin
