"""Application configuration.

All settings come from environment variables / .env with safe defaults so the
platform boots fully offline (demo mode) with zero configuration. Risk and
engine parameters are runtime-tunable copies persisted through the settings
API; the values here are the boot defaults.
"""
from __future__ import annotations

import contextvars
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
MODELS_DIR = DATA_DIR / "models"  # Kept for simple script access

_replay_models_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar("replay_models_dir", default=None)

def get_models_dir() -> Path:
    mock = _replay_models_dir.get()
    return mock if mock is not None else MODELS_DIR

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

VALID_MODES = ("demo", "paper", "live")
VALID_PROVIDERS = ("auto", "synthetic", "yahoo", "alpaca")
VALID_BROKERS = ("paper_sim", "alpaca")


class OpportunityConfig(BaseModel):
    """Cost-aware opportunity controls; never overrides risk or execution gates."""
    enabled: bool = True
    min_net_edge_bps: float = Field(15.0, ge=0.0, le=10_000.0)
    # =============================================================================
    # [MODIFIED 2026-10-22]: Calibrate min_reward_risk from 1.25 to 0.70
    # ORIGINAL INTENT: Required R:R >= 1.25 for all candidates.
    # REASON FOR CHANGE: Conflicted with ORB chop_reward_risk (0.75R) in RANGE/VOLATILE regimes,
    # causing 100% of chop breakouts (including winners like SOXS) to be vetoed.
    # SAFE ROLLBACK: Previous line preserved below as comment.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # min_reward_risk: float = Field(1.25, ge=0.1, le=100.0)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    min_reward_risk: float = Field(0.70, ge=0.1, le=100.0)
    # =============================================================================
    round_trip_cost_bps: float = Field(8.0, ge=0.0, le=10_000.0)
    overnight_enabled: bool = False
    overnight_max_allocation_pct: float = Field(15.0, ge=0.0, le=100.0)


class RiskConfig(BaseModel):
    """Runtime-tunable risk parameters. Tuned for active capital utilization (4% daily target)."""
    entry_order_type: str = Field("market", pattern="^(market|limit)$")
    risk_per_trade_pct: float = Field(0.5, ge=0.01, le=5.0)        # was 0.5 — 2× capital per trade
    max_position_pct: float = Field(20.0, ge=1.0, le=100.0)
    # =============================================================================
    # [MODIFIED 2026-10-22]: Increase max_open_positions from 8 to 10
    # ORIGINAL INTENT: max_open_positions = 8 capped concurrency.
    # REASON FOR CHANGE: Forensic audit shows 27 missed winners were blocked by portfolio/risk capacity.
    # Increasing to 10 provides room for multi-strategy execution without increasing per-trade risk.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # max_open_positions: int = Field(8, ge=1, le=50)                  # was 4 — 2× concurrent exposure
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    max_open_positions: int = Field(10, ge=1, le=50)                 # expanded to 10 for portfolio breadth
    # =============================================================================
    max_daily_loss_pct: float = Field(2.0, ge=0.1, le=25.0)         # was 2.0 — room for higher position count
    # DAILY PROFIT LOCK 2026-10-16 (operator demand: bank green days).
    # When day P&L reaches +lock_pct, engine flattens and blocks new entries
    # for the session — symmetric to the daily loss halt. 0.0 disables.
    # Rollback: 0.0. Env DAILY_PROFIT_LOCK_PCT.
    daily_profit_lock_pct: float = Field(1.0, ge=0.0, le=25.0)
    # PROFIT-LOCK DEFEND 2026-10-16: bank-and-ride mode lets winners run after
    # the lock; if the day then slips to this level, full-flatten to protect
    # the floor. 0.0 disables the defend (ride into close/EOD as usual).
    daily_profit_defend_pct: float = Field(0.5, ge=0.0, le=25.0)
    max_drawdown_pct: float = Field(10.0, ge=1.0, le=50.0)
    # =============================================================================
    # [MODIFIED 2026-10-23]: Asymmetric Re-Entry Lockout & Cooldown Calibration
    # ORIGINAL INTENT: symbol_cooldown_minutes was flat 30; no win consolidation or max losses per ticker.
    # REASON FOR CHANGE: Forensic audit proved flat/zero cooldown allowed NCPL to re-enter 4 minutes
    # after a loss for a -$465 blowout. Setting loss cooldown to 20m, win cooldown to 5m, and
    # symbol_max_daily_losses to 2 eliminates revenge trading while catching 2nd-leg momentum expansions.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # symbol_cooldown_minutes: int = Field(30, ge=0, le=390)                  # was 30
    # strategy_consecutive_loss_limit: int = Field(3, ge=1, le=20)            # was 3
    # strategy_cooldown_minutes: int = Field(30, ge=0, le=390)                # was 30
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    symbol_cooldown_minutes: int = Field(20, ge=0, le=390)                  # 20m cooldown on loss
    symbol_win_cooldown_minutes: int = Field(5, ge=0, le=390)               # 5m consolidation on win
    symbol_max_daily_losses: int = Field(2, ge=1, le=10)                    # session quarantine after 2 losses
    strategy_consecutive_loss_limit: int = Field(4, ge=1, le=20)            # 4 consecutive losses
    strategy_cooldown_minutes: int = Field(20, ge=0, le=390)                # 20m strategy pause
    min_bank_profit_usd: float = Field(100.0, ge=0.0)                       # $100 floor on 50% scaleouts
    # =============================================================================
    no_entry_minutes_before_close: int = Field(30, ge=0, le=180)     # was 30 — more trading time
    # --- NEW IMPLEMENTATION ---
    # Minutes before market close when positions must be flattened.
    flatten_minutes_before_close: int = Field(5, ge=0, le=60)
    # =============================================================================
    # [ADDED 2026-10-14]: CR-15 - Intelligent EOD Pre-Harvest Loss Threshold
    # ORIGINAL INTENT: Hardcoded -0.30R in engine_evaluation.py.
    # REASON FOR CHANGE: Expose configurable eod_preharvest_r in RiskConfig so operator
    # can tune pre-close scratch threshold without code edits.
    # SAFE ROLLBACK: Remove field.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    eod_preharvest_r: float = Field(-0.30, ge=-2.0, le=0.0)
    # =============================================================================
    # --- NEW IMPLEMENTATION ---
    # Calibrated to permit penny stock trading ($0.50+), allowing the bot to capture
    # volatile sub-$5.00 small-cap momentum runners and breakouts while maintaining
    # a sensible $5,000/min dollar-volume safety floor against completely dead tickers.
    min_price: float = Field(0.50, ge=0.0)
    min_avg_dollar_volume: float = Field(5_000.0, ge=0.0)
    # Market-data-gap entry block: no bar for N seconds during regular session
    # flags the symbol untradeable. Thin printers (user-added low-priced names)
    # legitimately go minutes between prints -- Aug-24 logged 34 false blocks.
    # 0 disables the gap block entirely.
    data_gap_threshold_secs: float = Field(120.0, ge=0.0, le=3900.0)
    ml_confidence_threshold: float = Field(0.52, ge=0.0, le=1.0)
    # =============================================================================
    # [MODIFIED 2026-10-22]: Calibrate regime_ml_thresholds and add panic_deploy_defensive_watchlist
    # ORIGINAL INTENT: PANIC=0.62 and STRESS=0.55 were unattainable ceilings that halted all trading.
    # REASON FOR CHANGE: Calibrating to 0.48-0.52 allows high-conviction breakdown and trend trades
    # to execute during volatile regimes. Adding panic_deploy_defensive_watchlist=False allows
    # dynamic candidate scanning instead of purging the universe for 5 stagnant ETFs.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # regime_ml_thresholds: dict[str, float] = Field(
    #     default={
    #         "NORMAL": 0.50,
    #         "CAUTION": 0.52,
    #         "STRESS": 0.55,
    #         "PANIC": 0.62,
    #     }
    # )
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    stress_ml_confidence_threshold: float = Field(0.52, ge=0.0, le=1.0)
    panic_deploy_defensive_watchlist: bool = Field(False)
    regime_ml_thresholds: dict[str, float] = Field(
        default={
            "NORMAL": 0.48,
            "CAUTION": 0.48,
            "STRESS": 0.50,
            "PANIC": 0.52,
        }
    )
    # =============================================================================
    min_specialist_auc: float = Field(0.53, ge=0.50, le=1.0)
    min_stop_pct: float = Field(0.5, ge=0.0, le=5.0)
    # =============================================================================
    # [ADDED 2026-10-23]: Volatility Noise Floor Cushioning & Sub-$5 Penny Stock Safeguards
    # ORIGINAL INTENT: Unbounded penny stock risk and micro-stops allowed 8,500+ shares of $2
    # stocks with 3-cent stops (HTZ, MGNX), causing 2-cent spread slips to blow up into -$430 losses.
    # REASON FOR CHANGE: User directive: "we can trade penny stocks, we shouldnt make those losses".
    # Volatility noise floor cushions stops to at least 0.60 ATR, while sub-$5 caps limit notional
    # to 10% equity ($10k) and risk to $250 with an 85% slippage buffer.
    # SAFE ROLLBACK: Remove the fields below.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    min_stop_floor_atr_mult: float = Field(0.60, ge=0.0, le=2.0)
    # max_stop_atr_mult = 0.0 disables artificial stop ceiling (matches Betadesk)
    max_stop_atr_mult: float = Field(0.0, ge=0.0)
    sub5_max_notional_pct: float = Field(10.0, ge=1.0, le=50.0)
    sub5_max_risk_dollars: float = Field(250.0, ge=50.0, le=2000.0)
    sub5_slippage_buffer: float = Field(0.85, ge=0.50, le=1.0)
    max_order_shares: int = Field(5000, ge=100, le=50000)
    # =============================================================================
    # =============================================================================
    # [MODIFIED 2026-10-24]: Calibrate score_threshold_range from 70.0 to 65.0 & Add Repeat-Loss / Runner Safeguards
    # ORIGINAL INTENT: score_threshold_range was 70.0. With 0.55 regime multiplier on breakout setups,
    # admission score topped out at 55.0, deadlocking the gate and rejecting 37 verified winners (+111.4 R).
    # Also added symbol_max_daily_losses (prevents MGNX repeat loss) and mega_win_cooldown (prevents PHAT giveback).
    # SAFE ROLLBACK: The original code is preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # score_threshold_trend: float = Field(55.0, ge=0.0, le=100.0)    # was 55.0
    # score_threshold_range: float = Field(70.0, ge=0.0, le=100.0)    # was 70.0
    # score_threshold_volatile: float = Field(65.0, ge=0.0, le=100.0) # was 65.0
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    score_threshold_trend: float = Field(55.0, ge=0.0, le=100.0)
    score_threshold_range: float = Field(65.0, ge=0.0, le=100.0)
    score_threshold_volatile: float = Field(65.0, ge=0.0, le=100.0)
    symbol_max_daily_losses: int = Field(1, ge=1, le=10)
    mega_win_cooldown_r: float = Field(3.0, ge=1.0, le=10.0)
    mega_win_cooldown_minutes: int = Field(45, ge=5, le=240)
    # =============================================================================
    score_threshold_event: float = Field(75.0, ge=0.0, le=100.0)    # was 75.0
    kelly_fraction_cap: float = Field(0.20, ge=0.01, le=0.50)       # was 0.15
    max_positions_per_sector: int = Field(5, ge=1, le=10)           # was 2
    max_correlation_penalty: float = Field(0.5, ge=0.0, le=1.0)
    harvest_risk_multiplier: float = Field(3.0, ge=1.0, le=5.0)     # was 2.5 — bigger A+ sizing
    scanner_watchlist_ttl: int = Field(15, ge=1, le=30)             # was 3 — hold scanner symbols longer
    # =============================================================================
    # [MODIFIED 2026-10-14]: CR-18 & CR-19 - Morning Turnover Split & Countertrend Risk Mult
    # ORIGINAL INTENT: Single max_daily_turnover_multiple of 5.0x across whole day; no countertrend mult.
    # REASON FOR CHANGE:
    # 1. CR-18: Split turnover budget into morning (09:30-12:00) max 3.0x and full-day 5.0x so
    #    morning churn does not consume afternoon trend opportunities.
    # 2. CR-19: Countertrend sizing multiplier (0.5) to scale down shorts in TREND_UP or longs in TREND_DOWN.
    # SAFE ROLLBACK: The original code is removed; rollback via git history.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    # =============================================================================
    # [MODIFIED 2026-10-23]: Calibrate Turnover Multiples to Unlock Shadow Missed Winners
    # ORIGINAL INTENT: max_morning 3.0x and max_daily 5.0x froze trading after 3 morning trades.
    # REASON FOR CHANGE: Shadow ledger audit proved 113+ winning trades (over 150% PnL, including
    # USDE +14.69%, FIGS +7.64%, NBIS +5.60%, PLTR +5.56%) were blocked when morning turnover reached 2.39x.
    # Calibrating morning to 10.0x and daily to 20.0x prevents mid-morning freeze while guarding against runaway loops.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # max_daily_turnover_multiple: float = Field(5.0, ge=0.1, le=100.0)
    # max_morning_turnover_multiple: float = Field(3.0, ge=0.1, le=100.0)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    max_daily_turnover_multiple: float = Field(20.0, ge=0.1, le=100.0)
    max_morning_turnover_multiple: float = Field(10.0, ge=0.1, le=100.0)
    # =============================================================================
    countertrend_mult: float = Field(0.5, ge=0.1, le=1.0)
    # =============================================================================
    # Fast-replay only: minimum session-time seconds between full market-risk
    # family evaluations (live path always evaluates every tick). Bars arrive
    # at 60s cadence, so the default evaluates every other bar.
    replay_market_risk_eval_interval_sec: int = Field(120, ge=0, le=3600)
    # --- Recent Performance Governor (evidence-driven entry-risk scaling) ---
    # Converts the platform's OWN recent closed-trade expectancy per
    # symbol/strategy/session-hour into a conservative sizing multiplier.
    # Can only tighten (<= 1.0); adapts automatically as performance changes.
    governor_enabled: bool = Field(True)
    governor_lookback_days: int = Field(14, ge=1, le=90)
    governor_min_trades: int = Field(6, ge=1, le=100)
    governor_shrink_k: float = Field(8.0, ge=0.0, le=100.0)
    governor_loss_scale: float = Field(2.2, ge=0.0, le=100.0)
    governor_floor: float = Field(0.25, ge=0.05, le=1.0)
    governor_refresh_minutes: int = Field(15, ge=1, le=240)
    # --- Entry chase veto (evidence-driven) ---
    # Measured on live history: trades whose DECISION bar was a vertical candle
    # (range >= 1.8x its own ATR14) closing at the directional extreme went
    # never-green 77% of the time and lost -49R net -- buying spent moves.
    # Rejects those candidates outright. Applies uniformly to every strategy;
    # tightens admission, changes no existing threshold.
    enable_entry_chase_veto: bool = Field(True)
    entry_chase_tr_atr_mult: float = Field(1.8, ge=0.5, le=10.0)
    entry_chase_close_pos: float = Field(0.65, ge=0.5, le=1.0)
    # Entry location veto: reject entries when price is stretched too far from
    # session VWAP in the trade direction (mathematically exhausted; inverted
    # reward:risk). Sibling-bot evidence + our own 68% never-green entries.
    enable_entry_location_veto: bool = Field(True)
    entry_max_vwap_stretch_atr: float = Field(3.0, ge=0.5, le=20.0)
    # =============================================================================
    # [MODIFIED 2026-10-22]: Calibrate entry_vwap_stretch_trend_mult from 1.0 to 1.6
    # ORIGINAL INTENT: entry_vwap_stretch_trend_mult = 1.0 kept trend days locked to the same 3.0 ATR
    # limit as range days.
    # REASON FOR CHANGE: Shadow ledger audit shows genuine trend breakout runners (SAIQ +4.47%, NBIS +4.01%)
    # detach from VWAP (>3.0 ATR) in sustained trends and were vetoed. 1.6 allows up to 4.8 ATR in
    # TREND_UP/TREND_DOWN while maintaining the strict 3.0 ATR discipline for RANGE days.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # entry_vwap_stretch_trend_mult: float = Field(1.0, ge=1.0, le=3.0)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    entry_vwap_stretch_trend_mult: float = Field(1.6, ge=1.0, le=3.0)
    # =============================================================================
    # Correlation clustering gate: block a new entry only when its 1-minute
    # close correlation with an open same-side position exceeds this.
    # Owner verdict 2026-10-15: default back to 0.70 -- the 18 same-day blocks
    # were genuine same-side clusters, and breadth is not worth cluster risk
    # without profit evidence. Same-side scoping (hedges exempt) is retained:
    # a hedge is diversification, not clustering, at any threshold.
    max_position_correlation: float = Field(0.70, ge=0.5, le=1.0)
    # Opening blackout: skip NEW entries in the first N minutes after the bell.
    # Measured on exact-path backtest: 0-5min entries were 6/6 losers (-$910);
    # all positive expectancy begins ~minute 11. Tightening only.
    enable_opening_blackout: bool = Field(True)
    entry_blackout_minutes_from_open: int = Field(10, ge=0, le=120)
    # Session-extreme proximity guard: block momentum entries INTO a session
    # extreme. Shorts within K x ATR14 of the session low (longs of the high)
    # are late continuation chases buying an already-spent multi-bar move --
    # the single decision-bar chase veto cannot see this class. Breakout
    # strategies and mean reversion strategies are exempt.
    enable_entry_extreme_guard: bool = Field(True)
    entry_max_extreme_proximity_atr: float = Field(1.0, ge=0.5, le=20.0)
    # Entry consensus (breakout strategies): MEASURED & FALSIFIED on Aug 10-21
    # exact-path backtest -- requiring RS>=50 for momentum longs halved trade
    # count and cut net from +$3.6k to +$0.3k (weak-RS laggard catch-ups were
    # profitable). Kept as an opt-in experiment knob, default OFF.
    enable_entry_consensus: bool = Field(False)
    entry_consensus_rs_long_min: float = Field(50.0, ge=0.0, le=100.0)
    adaptive_model_weight: float = Field(0.30, ge=0.0, le=1.0)
    # Manual-entry session budget: undisciplined discretionary taps cost
    # -$3,909 across 6 manual trades (CR-21). Entries beyond this per-session
    # count are rejected for operator review. Closes/flattens are NEVER
    # blocked -- risk reduction always flows. 0 disables manual entries.
    max_manual_entries_per_session: int = Field(3, ge=0, le=20)
    # =============================================================================
    # [ADDED 2026-10-14]: CR-02b - Pullback Re-Arm Global Toggle Knob
    # ORIGINAL INTENT: Registered strategy with no config-level enable flag.
    # REASON FOR CHANGE: Expose configurable enable_pullback_rearm in RiskConfig so operator
    # can cleanly enable/disable second-chance continuation entries.
    # SAFE ROLLBACK: Remove field.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    enable_pullback_rearm: bool = Field(True)
    # =============================================================================


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8",
        extra="ignore", case_sensitive=False,
    )

    strategy_config: dict[str, dict[str, Any]] = Field(
        default={
            "momentum": {"enabled": True, "params": {}},
            "mean_reversion": {"enabled": True, "params": {}},
            "breakout": {"enabled": True, "params": {}},
            "vwap_cross": {"enabled": True, "params": {}},
            "orb": {"enabled": True, "params": {}},
            "bb_squeeze": {"enabled": True, "params": {}},
            "gap_and_go": {"enabled": True, "params": {}},
            "trend_pullback": {"enabled": True, "params": {}},
            "liquidity_sweep": {"enabled": True, "params": {}},
            "relative_strength": {"enabled": True, "params": {}},
        }
    )

    host: str = Field("127.0.0.1", validation_alias=AliasChoices("ALPHADESK_HOST", "HOST"))
    port: int = Field(8742, validation_alias=AliasChoices("ALPHADESK_PORT", "PORT"))
    mode: str = Field("demo", validation_alias=AliasChoices("ALPHADESK_MODE", "MODE"))
    data_provider: str = Field("auto", validation_alias="DATA_PROVIDER")
    broker: str = Field("paper_sim", validation_alias="BROKER")
    alpaca_api_key: str = Field("", validation_alias="ALPACA_API_KEY")
    alpaca_api_secret: str = Field("", validation_alias="ALPACA_API_SECRET")
    alpaca_paper: bool = Field(True, validation_alias="ALPACA_PAPER")
    enable_live_trading: bool = Field(False, validation_alias="ENABLE_LIVE_TRADING")
    slack_webhook_url: str = Field("", validation_alias="SLACK_WEBHOOK_URL")

    entry_order_type: str = Field("market", validation_alias="ENTRY_ORDER_TYPE")
    risk_per_trade_pct: float = Field(1.0, validation_alias="RISK_PER_TRADE_PCT")           # was 0.5
    max_position_pct: float = Field(20.0, validation_alias="MAX_POSITION_PCT")
    # =============================================================================
    # [MODIFIED 2026-10-22]: Increase Settings.max_open_positions to 10 and add entry_vwap_stretch_trend_mult
    # ORIGINAL INTENT: max_open_positions was 8 in Settings.
    # REASON FOR CHANGE: Sync Settings with RiskConfig capacity increase to 10 and provide
    # validation_alias for ENTRY_VWAP_STRETCH_TREND_MULT.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # max_open_positions: int = Field(8, validation_alias="MAX_OPEN_POSITIONS")                # was 4
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    max_open_positions: int = Field(10, validation_alias="MAX_OPEN_POSITIONS")
    entry_vwap_stretch_trend_mult: float = Field(1.6, validation_alias="ENTRY_VWAP_STRETCH_TREND_MULT")
    # =============================================================================
    max_daily_loss_pct: float = Field(3.0, validation_alias="MAX_DAILY_LOSS_PCT")            # was 2.0
    daily_profit_lock_pct: float = Field(1.0, validation_alias="DAILY_PROFIT_LOCK_PCT")
    daily_profit_defend_pct: float = Field(0.5, validation_alias="DAILY_PROFIT_DEFEND_PCT")
    max_drawdown_pct: float = Field(10.0, validation_alias="MAX_DRAWDOWN_PCT")
    symbol_cooldown_minutes: int = Field(10, validation_alias="COOLDOWN_MINUTES")                   # was 30
    strategy_consecutive_loss_limit: int = Field(4, validation_alias="CONSECUTIVE_LOSS_LIMIT")        # was 3
    strategy_cooldown_minutes: int = Field(10, validation_alias="STRATEGY_COOLDOWN_MINUTES")
    no_entry_minutes_before_close: int = Field(15, validation_alias="NO_ENTRY_MINUTES_BEFORE_CLOSE")  # was 30
    flatten_minutes_before_close: int = Field(5, validation_alias="FLATTEN_MINUTES_BEFORE_CLOSE")
    # --- NEW IMPLEMENTATION ---
    # Calibrated to permit penny stock trading ($0.50+) and small-cap momentum runners.
    min_price: float = Field(0.50, validation_alias="MIN_PRICE")
    min_avg_dollar_volume: float = Field(5_000.0, validation_alias="MIN_AVG_DOLLAR_VOLUME")
    data_gap_threshold_secs: float = Field(120.0, validation_alias="DATA_GAP_THRESHOLD_SECS")
    min_specialist_auc: float = Field(0.53, validation_alias="MIN_SPECIALIST_AUC")
    min_stop_pct: float = Field(0.5, validation_alias="MIN_STOP_PCT")
    min_stop_floor_atr_mult: float = Field(0.60, validation_alias="MIN_STOP_FLOOR_ATR_MULT")
    max_stop_atr_mult: float = Field(0.0, validation_alias="MAX_STOP_ATR_MULT")
    sub5_max_notional_pct: float = Field(10.0, validation_alias="SUB5_MAX_NOTIONAL_PCT")
    sub5_max_risk_dollars: float = Field(250.0, validation_alias="SUB5_MAX_RISK_DOLLARS")
    sub5_slippage_buffer: float = Field(0.85, validation_alias="SUB5_SLIPPAGE_BUFFER")
    max_order_shares: int = Field(5000, validation_alias="MAX_ORDER_SHARES")
    # =============================================================================
    # [ADDED 2026-10-24]: Settings validation aliases for repeat-loss and mega-win cooldowns
    # -----------------------------------------------------------------------------
    symbol_max_daily_losses: int = Field(1, validation_alias="SYMBOL_MAX_DAILY_LOSSES")
    mega_win_cooldown_r: float = Field(3.0, validation_alias="MEGA_WIN_COOLDOWN_R")
    mega_win_cooldown_minutes: int = Field(45, validation_alias="MEGA_WIN_COOLDOWN_MINUTES")
    # =============================================================================
    kelly_fraction_cap: float = Field(0.15, validation_alias="KELLY_FRACTION_CAP")
    max_positions_per_sector: int = Field(2, validation_alias="MAX_POSITIONS_PER_SECTOR")
    max_correlation_penalty: float = Field(0.5, validation_alias="MAX_CORRELATION_PENALTY")
    harvest_risk_multiplier: float = Field(2.5, validation_alias="HARVEST_RISK_MULTIPLIER")
    score_threshold_trend: float = Field(55.0, validation_alias="SCORE_THRESHOLD_TREND")
    score_threshold_range: float = Field(70.0, validation_alias="SCORE_THRESHOLD_RANGE")
    score_threshold_volatile: float = Field(65.0, validation_alias="SCORE_THRESHOLD_VOLATILE")
    score_threshold_event: float = Field(75.0, validation_alias="SCORE_THRESHOLD_EVENT")

    paper_starting_cash: float = Field(100_000.0, validation_alias="PAPER_STARTING_CASH")
    paper_slippage_bps: float = Field(2.0, validation_alias="PAPER_SLIPPAGE_BPS")
    # =============================================================================
    # [MODIFIED 2026-10-22]: Restore poll_interval_sec in Settings
    # ORIGINAL INTENT: Settings defines operational timing including poll_interval_sec.
    # REASON FOR CHANGE: Missing poll_interval_sec caused engine._run_loop to crash on startup
    # with AttributeError in paper and live modes, preventing the engine from executing any ticks.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # paper_commission_per_share: float = Field(0.0, validation_alias="PAPER_COMMISSION_PER_SHARE")
    # paper_minimum_commission: float = Field(0.0, validation_alias="PAPER_MINIMUM_COMMISSION")
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    paper_commission_per_share: float = Field(0.0, validation_alias="PAPER_COMMISSION_PER_SHARE")
    paper_minimum_commission: float = Field(0.0, validation_alias="PAPER_MINIMUM_COMMISSION")
    poll_interval_sec: float = Field(5.0, validation_alias="POLL_INTERVAL_SEC")
    # =============================================================================
    opportunity_enabled: bool = Field(True, validation_alias="OPPORTUNITY_ENABLED")
    opportunity_min_net_edge_bps: float = Field(15.0, validation_alias="OPPORTUNITY_MIN_NET_EDGE_BPS")
    # =============================================================================
    # [MODIFIED 2026-10-22]: Calibrate Settings.opportunity_min_reward_risk from 1.25 to 0.70
    # ORIGINAL INTENT: Required R:R >= 1.25 in Settings.
    # REASON FOR CHANGE: Permits regime-adapted 0.75R targets from strategies like ORB in chop regimes.
    # SAFE ROLLBACK: Previous line preserved below as comment.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # opportunity_min_reward_risk: float = Field(1.25, validation_alias="OPPORTUNITY_MIN_REWARD_RISK")
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    opportunity_min_reward_risk: float = Field(0.70, validation_alias="OPPORTUNITY_MIN_REWARD_RISK")
    # =============================================================================
    opportunity_round_trip_cost_bps: float = Field(8.0, validation_alias="OPPORTUNITY_ROUND_TRIP_COST_BPS")
    overnight_enabled: bool = Field(False, validation_alias="OVERNIGHT_ENABLED")
    overnight_max_allocation_pct: float = Field(15.0, validation_alias="OVERNIGHT_MAX_ALLOCATION_PCT")
    demo_bar_seconds: float = Field(5.0, validation_alias="DEMO_BAR_SECONDS")
    default_watchlist: str = Field("SPY,QQQ,AAPL,NVDA,TSLA,MSFT,AMD,META,AMZN,AVGO,BABA,PLTR,ARM", validation_alias="DEFAULT_WATCHLIST")
    # =============================================================================
    # [MODIFIED 2026-10-22]: Calibrate Settings.ml_confidence_threshold and add stress/panic fields
    # ORIGINAL INTENT: ml_confidence_threshold was 0.55.
    # REASON FOR CHANGE: Calibrate baseline to 0.48 and add stress_ml_confidence_threshold (0.52)
    # and panic_deploy_defensive_watchlist (False) to support dynamic trading during volatility.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # ml_confidence_threshold: float = Field(0.55, validation_alias="ML_CONFIDENCE_THRESHOLD")
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    ml_confidence_threshold: float = Field(0.48, validation_alias="ML_CONFIDENCE_THRESHOLD")
    stress_ml_confidence_threshold: float = Field(0.52, validation_alias="STRESS_ML_CONFIDENCE_THRESHOLD")
    panic_deploy_defensive_watchlist: bool = Field(False, validation_alias="PANIC_DEPLOY_DEFENSIVE_WATCHLIST")
    # =============================================================================
    ml_horizon_bars: int = Field(15, validation_alias="ML_HORIZON_BARS")
    gemini_api_key: str = Field("", validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    # 90+10 session window must fit inside the trainer's fetch span
    # (ALPHADESK_TRAIN_DAYS=180 calendar days ~= 126 ET sessions). The old
    # 120-day default needed 130 sessions -> "Only 0 windows" every cycle
    # and promotion AUCs silently computed on full-dataset fits (2026-08-24).
    walk_forward_train_days: int = Field(90, validation_alias="WALK_FORWARD_TRAIN_DAYS")
    walk_forward_val_days: int = Field(10, validation_alias="WALK_FORWARD_VAL_DAYS")
    walk_forward_min_windows: int = Field(3, validation_alias="WALK_FORWARD_MIN_WINDOWS")
    # Training is a local batch job: default to the immutable local bar cache
    # and never hit provider rate limits (429 backoff loops stalled cycles).
    train_cache_only: bool = Field(True, validation_alias="TRAIN_CACHE_ONLY")
    trade_score_w1: float = Field(0.30, validation_alias="TRADE_SCORE_W1")
    trade_score_w2: float = Field(0.25, validation_alias="TRADE_SCORE_W2")
    trade_score_w3: float = Field(0.20, validation_alias="TRADE_SCORE_W3")
    trade_score_w4: float = Field(0.10, validation_alias="TRADE_SCORE_W4")
    trade_score_w5: float = Field(0.10, validation_alias="TRADE_SCORE_W5")
    trade_score_w6: float = Field(0.05, validation_alias="TRADE_SCORE_W6")
    dq_max_gap_minutes: int = Field(5, validation_alias="DQ_MAX_GAP_MINUTES")
    dq_max_spread_pct: float = Field(0.5, validation_alias="DQ_MAX_SPREAD_PCT")
    # =============================================================================
    # [MODIFIED 2026-10-23]: Expose Calibrated Turnover Multiples in Settings
    # ORIGINAL INTENT: max_daily was 5.0; max_morning was not aliased in Settings.
    # REASON FOR CHANGE: Expose MAX_MORNING_TURNOVER_MULTIPLE=10.0 and MAX_DAILY_TURNOVER_MULTIPLE=20.0.
    # SAFE ROLLBACK: Previous line preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # max_daily_turnover_multiple: float = Field(5.0, validation_alias="MAX_DAILY_TURNOVER_MULTIPLE")
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    max_daily_turnover_multiple: float = Field(20.0, validation_alias="MAX_DAILY_TURNOVER_MULTIPLE")
    max_morning_turnover_multiple: float = Field(10.0, validation_alias="MAX_MORNING_TURNOVER_MULTIPLE")
    # =============================================================================
    ml_min_promotable_auc: float = Field(0.53, validation_alias="ML_MIN_PROMOTABLE_AUC")
    eod_post_close_grace_minutes: int = Field(15, validation_alias="EOD_POST_CLOSE_GRACE_MINUTES")
    dq_min_bars_warmup: int = Field(50, validation_alias="DQ_MIN_BARS_WARMUP")
    # =============================================================================
    # [MODIFIED 2026-10-23]: Expose Asymmetric Cooldowns and Min Bank Floor in Settings
    # ORIGINAL INTENT: Not exposed in Settings class.
    # REASON FOR CHANGE: Expose symbol_win_cooldown_minutes, symbol_max_daily_losses, and min_bank_profit_usd
    # to environment configuration.
    # SAFE ROLLBACK: Remove fields.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    symbol_win_cooldown_minutes: int = Field(5, validation_alias="SYMBOL_WIN_COOLDOWN_MINUTES")
    symbol_max_daily_losses: int = Field(2, validation_alias="SYMBOL_MAX_DAILY_LOSSES")
    min_bank_profit_usd: float = Field(100.0, validation_alias="BOT_MIN_BANK_PROFIT_USD")
    # =============================================================================
    db_path: str = Field("", validation_alias="ALPHADESK_DB_PATH")
    admin_token: str = Field("", validation_alias="ALPHADESK_ADMIN_TOKEN")

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        if self.mode not in VALID_MODES:
            raise ValueError(f"ALPHADESK_MODE must be one of {VALID_MODES}, got {self.mode!r}")
        if self.data_provider not in VALID_PROVIDERS:
            raise ValueError(f"DATA_PROVIDER must be one of {VALID_PROVIDERS}, got {self.data_provider!r}")
        if self.broker not in VALID_BROKERS:
            raise ValueError(f"BROKER must be one of {VALID_BROKERS}, got {self.broker!r}")

    @property
    def has_alpaca_keys(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_api_secret)

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path) if self.db_path else DATA_DIR / "alphadesk.db"

    def resolve_data_provider(self, mode: str | None = None) -> str:
        mode = mode or self.mode
        if mode == "demo":
            return "synthetic"
        if self.data_provider != "auto":
            return self.data_provider
        return "alpaca" if self.has_alpaca_keys else "yahoo"

    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.default_watchlist.split(",") if s.strip()]

    def risk_config(self) -> RiskConfig:
        """Build the canonical boot RiskConfig without silently dropping fields."""
        # =============================================================================
        # [MODIFIED 2026-10-14]: L46 & GAP-23 - Prevent Dropping RiskConfig Fields
        # ORIGINAL INTENT: Hand-enumerated ~25 fields, dropping 20+ fields (governor, vetoes,
        # turnover multiples, notional caps) on reload.
        # REASON FOR CHANGE: Automatically map all fields shared between Settings and RiskConfig,
        # with fallback to RiskConfig defaults for any remaining unmapped fields.
        # SAFE ROLLBACK: The original code is removed; rollback via git history.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        field_kwargs = {
            f: getattr(self, f)
            for f in RiskConfig.model_fields
            if hasattr(self, f)
        }
        return RiskConfig(**field_kwargs)

    def opportunity_config(self) -> OpportunityConfig:
        return OpportunityConfig(
            enabled=self.opportunity_enabled,
            min_net_edge_bps=self.opportunity_min_net_edge_bps,
            min_reward_risk=self.opportunity_min_reward_risk,
            round_trip_cost_bps=self.opportunity_round_trip_cost_bps,
            overnight_enabled=self.overnight_enabled,
            overnight_max_allocation_pct=self.overnight_max_allocation_pct,
        )
        # =============================================================================

    def trade_score_weights(self) -> tuple[float, float, float, float, float, float]:
        raw = [self.trade_score_w1, self.trade_score_w2, self.trade_score_w3, self.trade_score_w4, self.trade_score_w5, self.trade_score_w6]
        total = sum(raw) or 1.0
        return tuple(w / total for w in raw)  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
