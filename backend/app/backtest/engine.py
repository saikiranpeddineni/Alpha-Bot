"""Event-driven exact replay backtest engine.

Forces the live TradingEngine to tick through historical bars,
guaranteeing 100% execution parity with production.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from app.ai.features import compute_features
from app.backtest.metrics import compute_metrics
from app.brokers.paper import PaperBroker
from app.config import RiskConfig, Settings
from app.core.clock import MarketClock
from app.core.domain import EngineState, TradeRecord, new_id, utcnow
from app.core.engine import TradingEngine
try:
    from app.core.logging import applog
except ImportError:
    from app.core.applog import applog
from app.data.manager import DataManager

MAX_CURVE_POINTS = 2000


class BacktestParams(BaseModel):
    symbols: list[str]
    start: datetime
    end: datetime
    starting_cash: float = 100_000.0
    slippage_bps: float = 2.0
    use_ml_gate: bool = True
    disable_scanner: bool = False
    strategies: dict[str, dict] | None = None
    risk: RiskConfig | None = None
    # Scanner anchors (the user's configured watchlist); defaults to `symbols`.
    core_watchlist: list[str] | None = None


class BacktestResult(BaseModel):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    params: dict
    metrics: dict
    equity_curve: list[tuple[datetime, float]]
    trades: list[TradeRecord]
    bars_processed: int = 0
    warnings: list[str] = Field(default_factory=list)


def _downsample_curve(curve: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    if len(curve) <= MAX_CURVE_POINTS:
        return curve
    step = len(curve) / MAX_CURVE_POINTS
    sampled = [curve[int(i * step)] for i in range(MAX_CURVE_POINTS)]
    if sampled[-1] != curve[-1]:
        sampled.append(curve[-1])
    return sampled


class DummyProvider:
    name = "replay"


class ReplayDataManager:
    """Mock DataManager that feeds historical bars sequentially."""

    def __init__(self, frames: dict[str, pd.DataFrame], real_data: DataManager) -> None:
        self.provider = DummyProvider()
        self.frames = frames
        self.real_data = real_data
        self.all_ts = sorted(set().union(*[df.index for df in frames.values()]))
        self.current_idx = 0
        self.current_ts: pd.Timestamp | None = None

    async def poll(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        if self.current_idx >= len(self.all_ts):
            return {}

        ts = self.all_ts[self.current_idx]
        self.current_ts = ts
        self.current_idx += 1

        out = {}
        for sym, df in self.frames.items():
            if sym in symbols and ts in df.index:
                out[sym] = df.loc[[ts]]
        return out

    def frame(self, symbol: str, timeframe: str = "1Min", limit: int | None = None) -> pd.DataFrame:
        df = self.frames.get(symbol.upper())
        if df is None or df.empty or self.current_ts is None:
            return pd.DataFrame()

        # Resolution-safe cutoff: searchsorted handles ns/us index dtypes and
        # tz-aware comparisons natively (asi8/value mixes break on us-indexes).
        pos = int(df.index.searchsorted(self.current_ts, side="right"))
        df_up_to = df.iloc[:pos]
        if limit:
            return df_up_to.iloc[-limit:]
        return df_up_to

    async def warmup(self, symbols: list[str], lookback_bars: int = 800) -> None:
        pass

    async def history(
        self,
        symbol: str,
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        prefer_cache: bool = True,
    ) -> pd.DataFrame:
        """Delegate history calls (like the scanner's 1Day request) to the real data manager."""
        return await self.real_data.history(symbol, timeframe, start, end, prefer_cache=prefer_cache)

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        out = {}
        for sym in symbols:
            df = self.frame(sym)
            if not df.empty:
                out[sym] = float(df["close"].iloc[-1])
        return out

    async def close(self) -> None:
        pass


class ReplayClock(MarketClock):
    """Mock MarketClock that yields the current replay time."""
    def __init__(self):
        super().__init__(always_open=False)
        self.now = utcnow()

    def now_utc(self) -> datetime:
        return self.now


async def run_backtest(
    data: DataManager,
    model: Any,  # type hint relaxed to avoid circular import issues if any
    params: BacktestParams,
    on_progress: Callable[[float], None] | None = None,
) -> BacktestResult:
    warnings: list[str] = []

    # 1. Precompute features for the ENTIRE candidate pool so the dynamic scanner
    # can freely switch symbols mid-backtest.
    from app.core.scanner import CANDIDATE_POOL
    frames: dict[str, pd.DataFrame] = {}

    if params.disable_scanner:
        all_symbols = set([s.upper() for s in params.symbols])
    else:
        all_symbols = set([s.upper() for s in params.symbols] + CANDIDATE_POOL)

    # Fetch with a multi-day buffer so that at exactly params.start we already
    # have history. Must survive weekends (a Monday start minus 2 days lands on
    # Saturday -> zero warmup bars -> strategies indicator-blind at the open).
    # 5 days mirrors the live engine's warmup lookback.
    fetch_start = params.start - timedelta(days=5)

    t_load = time.time()
    # =============================================================================
    # [MODIFIED 2026-10-25]: Index Deduplication for Historical Replay Bars
    # ORIGINAL INTENT: Passed raw history directly to compute_features(raw).
    # REASON FOR CHANGE: If bars cache contains overlapping session records or duplicate timestamps,
    # pandas reindex in ta.enrich raises ValueError: cannot reindex on an axis with duplicate labels.
    # Deduplicating on index preserving the latest bar ensures clean, non-regressive feature generation.
    # SAFE ROLLBACK: Previous lines preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # for symbol in sorted(all_symbols):
    #     raw = await data.history(symbol, "1Min", fetch_start, params.end)
    #     if raw.empty or len(raw) < 60:
    #         if symbol in params.symbols:
    #             warnings.append(f"{symbol}: insufficient data ({len(raw)} bars) -- skipped")
    #             applog.warning("backtest", warnings[-1])
    #         continue
    #     frames[symbol] = compute_features(raw)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    for symbol in sorted(all_symbols):
        raw = await data.history(symbol, "1Min", fetch_start, params.end)
        if raw.empty or len(raw) < 60:
            if symbol in params.symbols:
                warnings.append(f"{symbol}: insufficient data ({len(raw)} bars) -- skipped")
                applog.warning("backtest", warnings[-1])
            continue
        if raw.index.has_duplicates:
            raw = raw[~raw.index.duplicated(keep="last")]
        frames[symbol] = compute_features(raw)
    # =============================================================================
    print(
        f"[load] {len(frames)} symbols, {sum(len(f) for f in frames.values()):,} bars, "
        f"{time.time() - t_load:.1f}s",
        flush=True,
    )

    if not frames:
        raise ValueError("No usable data for any requested symbol/date range")

    replay_data = ReplayDataManager(frames, data)

    # The fetch window intentionally includes a warmup buffer before
    # params.start; make sure the requested range itself contains tradable bars.
    if not any(ts >= params.start for ts in replay_data.all_ts):
        raise ValueError("No bars fall inside the requested start/end range")
    total_ticks = len(replay_data.all_ts)

    # 2. Wire up the EXACT live Trading Engine
    from app.db.database import Database

    settings = Settings(mode="paper", enable_live_trading=False)
    db = Database(Path(":memory:"))

    # Optional strategies overriding
    if params.strategies:
        db.set_setting("strategies", params.strategies)
    if params.risk:
        db.set_setting("risk_config", params.risk.model_dump())

    engine = TradingEngine(settings, db)
    risk_cfg = params.risk or RiskConfig()
    if getattr(engine, "gemini_council", None):
        engine.gemini_council.api_key = ""
        engine.gemini_council.enabled = bool(getattr(risk_cfg, "enable_ai_agent_gate", False))

    # 3. Inject our Mocks
    engine.mode = "paper"
    engine.user_watchlist = [s.upper() for s in (params.core_watchlist or params.symbols)]
    engine.watchlist = list(engine.user_watchlist)
    engine.clock = ReplayClock()
    engine.data = replay_data
    engine.broker = PaperBroker(
        starting_cash=params.starting_cash,
        slippage_bps=params.slippage_bps,
        fill_market_on="next_open",
    )
    engine.model = model
    from app.portfolio.tracker import PortfolioTracker
    engine.portfolio = PortfolioTracker()

    # Re-initialize the pipeline & strategies since we overwrote dependencies
    from app.risk.manager import RiskManager
    from app.strategies import SignalPipeline, create_strategies

    engine.risk = RiskManager(risk_cfg, engine.clock)
    engine.risk_config = risk_cfg

    # Only exempt requested symbols from volume filters if scanner is disabled (static backtest).
    # When scanner is active, allow the scanner's $1M/min liquidity filter to select liquid leaders.
    if params.disable_scanner:
        engine.risk.live_mover_symbols = set([s.upper() for s in params.symbols])
        engine.replay_scanner_enabled = False
        engine.scanner = None
        engine._scanner = None
    else:
        engine.risk.live_mover_symbols = set()
        engine.replay_scanner_enabled = True
        from app.core.scanner import MarketScanner
        engine.scanner = MarketScanner(
            data_manager=engine.data,
            core_watchlist=engine.user_watchlist,
            risk_config=risk_cfg,
        )
        engine._scanner = engine.scanner
        engine._scanner_data = engine.data

    engine.pipeline = SignalPipeline(model, risk_cfg.ml_confidence_threshold if params.use_ml_gate else 0.0)
    engine.strategies = create_strategies(params.strategies)

    engine.state = EngineState.RUNNING
    engine.silent = True  # replay must not build broadcast payloads per tick
    engine.replay_fast_path = True  # bypass ops monitors, run pure decision gates

    # 4. Tick through time
    bars_processed = 0

    # Pre-roll the first session
    engine.portfolio.roll_session(engine.clock.session_date(params.start), params.starting_cash)
    engine.portfolio.peak_equity = params.starting_cash

    for step in range(total_ticks):
        # We look into the future by 1 minute, just like TradingEngine does
        ts = replay_data.all_ts[step]

        # Only simulate ticks that are actually within the requested range
        if ts < params.start:
            replay_data.current_idx += 1
            replay_data.current_ts = ts
            continue

        engine.clock.now = (ts + timedelta(minutes=1)).to_pydatetime()

        await engine._tick(now=engine.clock.now)
        bars_processed += 1

        if on_progress and total_ticks and step % 200 == 0:
            on_progress(step / total_ticks)
            await asyncio.sleep(0)  # Yield control to the event loop so WebSocket messages are sent

    # Wrap up
    final_now = engine.clock.now + timedelta(minutes=2)
    await engine.broker.flatten_all(reason="backtest end")

    # Drain final fills
    while engine.broker.drain_fills():
        await engine._drain_fills(final_now)

    engine.portfolio.mark_equity(engine.broker.equity(), final_now)

    metrics = compute_metrics(engine.portfolio.equity_curve, engine.portfolio.trades, params.starting_cash)

    result = BacktestResult(
        params=params.model_dump(mode="json"),
        metrics=metrics,
        equity_curve=_downsample_curve(engine.portfolio.equity_curve),
        trades=engine.portfolio.trades,
        bars_processed=bars_processed,
        warnings=warnings,
    )
    applog.info(
        "backtest",
        f"Exact Replay done: {bars_processed} bars, {metrics['total_trades']} trades, "
        f"return {metrics['total_return_pct']}%, maxDD {metrics['max_drawdown_pct']}%",
    )
    return result
