"""Application configuration.

All settings come from environment variables / .env with safe defaults so the
platform boots fully offline (demo mode) with zero configuration. Risk and
engine parameters are runtime-tunable copies persisted through the settings
API; the values here are the boot defaults.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
MODELS_DIR = DATA_DIR / "models"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

VALID_MODES = ("demo", "paper", "live")
VALID_PROVIDERS = ("auto", "synthetic", "yahoo", "alpaca")
VALID_BROKERS = ("paper_sim", "alpaca")


class RiskConfig(BaseModel):
    """Runtime-tunable risk parameters. Defaults favor capital preservation."""
    risk_per_trade_pct: float = Field(0.5, ge=0.01, le=5.0)
    max_position_pct: float = Field(20.0, ge=1.0, le=100.0)
    max_open_positions: int = Field(4, ge=1, le=50)
    max_daily_loss_pct: float = Field(2.0, ge=0.1, le=25.0)
    max_drawdown_pct: float = Field(10.0, ge=1.0, le=50.0)
    consecutive_loss_limit: int = Field(3, ge=1, le=20)
    cooldown_minutes: int = Field(30, ge=1, le=390)
    no_entry_minutes_before_close: int = Field(30, ge=0, le=180)
    flatten_minutes_before_close: int = Field(5, ge=0, le=60)
    min_price: float = Field(5.0, ge=0.0)
    min_avg_dollar_volume: float = Field(100_000.0, ge=0.0)
    ml_confidence_threshold: float = Field(0.55, ge=0.0, le=1.0)
    min_specialist_auc: float = Field(0.53, ge=0.50, le=1.0)
    min_stop_pct: float = Field(0.5, ge=0.0, le=5.0)
    score_threshold_trend: float = Field(55.0, ge=0.0, le=100.0)
    score_threshold_range: float = Field(70.0, ge=0.0, le=100.0)
    score_threshold_volatile: float = Field(65.0, ge=0.0, le=100.0)
    score_threshold_event: float = Field(75.0, ge=0.0, le=100.0)
    kelly_fraction_cap: float = Field(0.15, ge=0.01, le=0.50)
    max_positions_per_sector: int = Field(2, ge=1, le=10)
    max_correlation_penalty: float = Field(0.5, ge=0.0, le=1.0)
    midday_score_boost: float = Field(15.0, ge=0.0, le=50.0)
    midday_start_et: int = Field(690, ge=570, le=960)
    midday_end_et: int = Field(795, ge=570, le=960)
    rsi2_base_fallback_bypass_enabled: bool = Field(True)
    rsi2_base_fallback_bypass_start_et: int = Field(660, ge=570, le=960)
    rsi2_base_fallback_bypass_end_et: int = Field(780, ge=570, le=960)
    scanner_watchlist_ttl: int = Field(3, ge=1, le=30)
    alternative_exits: list[dict] = Field(
        default=[
            {"policy_id": "exit_bracket_tight", "take_profit_pct": 1.0, "stop_loss_pct": 0.5},
            {"policy_id": "exit_bracket_wide", "take_profit_pct": 3.0, "stop_loss_pct": 1.5},
            {"policy_id": "exit_atr_trailing", "trailing_stop_atr_mult": 2.0},
        ]
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8",
        extra="ignore", case_sensitive=False,
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

    risk_per_trade_pct: float = Field(0.5, validation_alias="RISK_PER_TRADE_PCT")
    max_position_pct: float = Field(20.0, validation_alias="MAX_POSITION_PCT")
    max_open_positions: int = Field(4, validation_alias="MAX_OPEN_POSITIONS")
    max_daily_loss_pct: float = Field(2.0, validation_alias="MAX_DAILY_LOSS_PCT")
    max_drawdown_pct: float = Field(10.0, validation_alias="MAX_DRAWDOWN_PCT")
    consecutive_loss_limit: int = Field(3, validation_alias="CONSECUTIVE_LOSS_LIMIT")
    cooldown_minutes: int = Field(30, validation_alias="COOLDOWN_MINUTES")
    no_entry_minutes_before_close: int = Field(30, validation_alias="NO_ENTRY_MINUTES_BEFORE_CLOSE")
    flatten_minutes_before_close: int = Field(5, validation_alias="FLATTEN_MINUTES_BEFORE_CLOSE")
    min_price: float = Field(5.0, validation_alias="MIN_PRICE")
    min_avg_dollar_volume: float = Field(100_000.0, validation_alias="MIN_AVG_DOLLAR_VOLUME")
    min_specialist_auc: float = Field(0.53, validation_alias="MIN_SPECIALIST_AUC")
    min_stop_pct: float = Field(0.5, validation_alias="MIN_STOP_PCT")
    kelly_fraction_cap: float = Field(0.15, validation_alias="KELLY_FRACTION_CAP")
    max_positions_per_sector: int = Field(2, validation_alias="MAX_POSITIONS_PER_SECTOR")
    max_correlation_penalty: float = Field(0.5, validation_alias="MAX_CORRELATION_PENALTY")
    score_threshold_trend: float = Field(55.0, validation_alias="SCORE_THRESHOLD_TREND")
    score_threshold_range: float = Field(70.0, validation_alias="SCORE_THRESHOLD_RANGE")
    score_threshold_volatile: float = Field(65.0, validation_alias="SCORE_THRESHOLD_VOLATILE")
    score_threshold_event: float = Field(75.0, validation_alias="SCORE_THRESHOLD_EVENT")

    paper_starting_cash: float = Field(100_000.0, validation_alias="PAPER_STARTING_CASH")
    paper_slippage_bps: float = Field(2.0, validation_alias="PAPER_SLIPPAGE_BPS")
    paper_commission_per_share: float = Field(0.0, validation_alias="PAPER_COMMISSION_PER_SHARE")
    poll_interval_sec: float = Field(5.0, validation_alias="POLL_INTERVAL_SEC")
    demo_bar_seconds: float = Field(5.0, validation_alias="DEMO_BAR_SECONDS")
    default_watchlist: str = Field("SPY,QQQ,AAPL,NVDA,TSLA,MSFT,AMD,META", validation_alias="DEFAULT_WATCHLIST")

    ml_confidence_threshold: float = Field(0.55, validation_alias="ML_CONFIDENCE_THRESHOLD")
    ml_horizon_bars: int = Field(15, validation_alias="ML_HORIZON_BARS")
    gemini_api_key: str = Field("", validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    walk_forward_train_days: int = Field(60, validation_alias="WALK_FORWARD_TRAIN_DAYS")
    walk_forward_val_days: int = Field(10, validation_alias="WALK_FORWARD_VAL_DAYS")
    walk_forward_min_windows: int = Field(3, validation_alias="WALK_FORWARD_MIN_WINDOWS")
    trade_score_w1: float = Field(0.30, validation_alias="TRADE_SCORE_W1")
    trade_score_w2: float = Field(0.25, validation_alias="TRADE_SCORE_W2")
    trade_score_w3: float = Field(0.20, validation_alias="TRADE_SCORE_W3")
    trade_score_w4: float = Field(0.10, validation_alias="TRADE_SCORE_W4")
    trade_score_w5: float = Field(0.10, validation_alias="TRADE_SCORE_W5")
    trade_score_w6: float = Field(0.05, validation_alias="TRADE_SCORE_W6")
    dq_max_gap_minutes: int = Field(5, validation_alias="DQ_MAX_GAP_MINUTES")
    dq_max_spread_pct: float = Field(0.5, validation_alias="DQ_MAX_SPREAD_PCT")
    midday_score_boost: float = Field(15.0, validation_alias="MIDDAY_SCORE_BOOST")
    midday_start_et: int = Field(690, validation_alias="MIDDAY_START_ET")
    midday_end_et: int = Field(795, validation_alias="MIDDAY_END_ET")
    rsi2_base_fallback_bypass_enabled: bool = Field(True, validation_alias="RSI2_BASE_FALLBACK_BYPASS_ENABLED")
    rsi2_base_fallback_bypass_start_et: int = Field(660, validation_alias="RSI2_BASE_FALLBACK_BYPASS_START_ET")
    rsi2_base_fallback_bypass_end_et: int = Field(780, validation_alias="RSI2_BASE_FALLBACK_BYPASS_END_ET")
    dq_min_bars_warmup: int = Field(50, validation_alias="DQ_MIN_BARS_WARMUP")
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
        return RiskConfig(
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_position_pct=self.max_position_pct,
            max_open_positions=self.max_open_positions,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            consecutive_loss_limit=self.consecutive_loss_limit,
            cooldown_minutes=self.cooldown_minutes,
            no_entry_minutes_before_close=self.no_entry_minutes_before_close,
            flatten_minutes_before_close=self.flatten_minutes_before_close,
            min_price=self.min_price,
            min_avg_dollar_volume=self.min_avg_dollar_volume,
            ml_confidence_threshold=self.ml_confidence_threshold,
            min_specialist_auc=self.min_specialist_auc,
            min_stop_pct=self.min_stop_pct,
            score_threshold_trend=self.score_threshold_trend,
            score_threshold_range=self.score_threshold_range,
            score_threshold_volatile=self.score_threshold_volatile,
            score_threshold_event=self.score_threshold_event,
            midday_score_boost=self.midday_score_boost,
            midday_start_et=self.midday_start_et,
            midday_end_et=self.midday_end_et,
            rsi2_base_fallback_bypass_enabled=self.rsi2_base_fallback_bypass_enabled,
            rsi2_base_fallback_bypass_start_et=self.rsi2_base_fallback_bypass_start_et,
            rsi2_base_fallback_bypass_end_et=self.rsi2_base_fallback_bypass_end_et,
            kelly_fraction_cap=self.kelly_fraction_cap,
            max_positions_per_sector=self.max_positions_per_sector,
            max_correlation_penalty=self.max_correlation_penalty,
        )

    def trade_score_weights(self) -> tuple[float, float, float, float, float, float]:
        raw = [self.trade_score_w1, self.trade_score_w2, self.trade_score_w3, self.trade_score_w4, self.trade_score_w5, self.trade_score_w6]
        total = sum(raw) or 1.0
        return tuple(w / total for w in raw)  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
