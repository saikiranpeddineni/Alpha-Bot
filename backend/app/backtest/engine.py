"""Event-driven backtest engine.

Replays historical 1-minute bars through the exact same components live
trading uses -- strategies, AI signal pipeline, risk manager, paper fill
simulator, portfolio tracker -- so a backtest is a faithful dry run, not a
separate implementation.

Honesty rules:
- market entries queue and fill at the NEXT bar's open (no look-ahead)
- stops assumed to fill before targets when one bar touches both
- stops pay adverse slippage; indicators/features are strictly causal
"""

from __future__ import annotations

from collections.abc import Callable
import asyncio
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field

from app.ai.features import compute_features
from app.indicators.ta import compute_relative_strength
from app.ai.model import SignalModel
from app.ai.regime import detect_regime
from app.backtest.metrics import compute_metrics
from app.brokers.paper import PaperBroker
from app.config import RiskConfig
from app.core.clock import MarketClock
from app.core.domain import SignalAction, TradeRecord, new_id, utcnow, OrderStatus
from app.core.applog import applog
from app.data.manager import DataManager
from app.data.base import HistoricalDataCoverageError
from app.portfolio.tracker import PortfolioTracker
from app.risk.manager import AccountContext, RiskManager
from app.strategies import SignalPipeline, create_strategies
from app.strategies.base import StrategyContext

from app.indicators.slot_volume import SlotVolumeEngine
from app.indicators.vwap_telemetry import compute_vwap_telemetry
from app.core.shadow_ledger import ShadowLedger
from app.core.counterfactual import ExitCounterfactualEngine
from app.ai.gate_report import build_gate_value_report


SLICE_WINDOW = 450  # bars of context handed to strategies (full session + warmup)
MAX_CURVE_POINTS = 2000


class BacktestParams(BaseModel):
    symbols: list[str]
    start: datetime
    end: datetime
    starting_cash: float = 100_000.0
    slippage_bps: float = 2.0
    use_ml_gate: bool = True
    use_dynamic_scanner: bool = False
    debug_mode: bool = False
    strategies: dict[str, dict] | None = None  # {name: {enabled: bool, params: {...}}}
    risk: RiskConfig | None = None
    use_corrected_trailing: bool = True
    ignore_temporal_leakage: bool = False
    model_eligibility_mode: str = "walk_forward"  # "walk_forward" or "retrospective_current_model"
    cache_policy: str = "default"  # "default" or "immutable_cache_only"
    verify_dataset_immutability: bool = False


class BacktestResult(BaseModel):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    params: dict
    metrics: dict
    equity_curve: list[tuple[datetime, float]]
    trades: list[TradeRecord]
    bars_processed: int = 0
    warnings: list[str] = Field(default_factory=list)
    trailing_exits: list[dict] = Field(default_factory=list)
    shadow_signals: list[dict] = Field(default_factory=list)
    counterfactuals: list[dict] = Field(default_factory=list)
    gate_value_report: dict = Field(default_factory=dict)
    
    # New run manifest fields
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    actual_data_start: datetime | None = None
    actual_data_end: datetime | None = None
    requested_session_count: int = 0
    actual_session_count: int = 0
    shared_session_count: int = 0
    data_source_mode: str = "CACHE_ONLY"
    config_sha256: str = ""
    code_state_sha256: str = ""
    dataset_sha256: str = ""
    model_sha256: str = ""
    decision_artifacts_sha256: str = ""
    provenance: dict = Field(default_factory=dict)



def compute_code_state_sha256() -> str:
    import hashlib
    from pathlib import Path
    
    app_dir = Path(__file__).resolve().parent.parent # app directory (backend/app)
    files_to_hash = [
        "app/backtest/engine.py",
        "app/backtest/metrics.py",
        "app/core/clock.py",
        "app/core/domain.py",
        "app/strategies/__init__.py",
        "app/strategies/base.py",
        "app/strategies/pipeline.py",
        "app/strategies/orb.py",
        "app/strategies/momentum.py",
        "app/strategies/vwap_reversion.py",
        "app/strategies/rsi_reversion.py",
        "app/strategies/bb_squeeze.py",
        "app/ai/features.py",
        "app/indicators/ta.py",
        "app/ai/model.py",
        "app/ai/prediction.py",
        "app/config.py",
    ]
    h = hashlib.sha256()
    for rel_path in sorted(files_to_hash):
        full_path = app_dir.parent / rel_path
        if full_path.exists():
            with open(full_path, "rb") as f:
                h.update(f.read())
        else:
            h.update(f"MISSING:{rel_path}".encode())
    return h.hexdigest()


def get_file_sha256(path: Path | None) -> str:
    if path is None or not path.exists():
        return "NOT_FOUND"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_decision_artifacts_manifest(
    models_dir: Path | None,
    active_policy: dict | None = None,
    challenger_policies: list | None = None,
) -> tuple[dict, str]:
    import hashlib
    import json
    manifest = {
        "base_models": {},
        "specialist_models": {},
        "meta_label_models": {},
        "active_policy": active_policy or {},
        "challengers": challenger_policies or [],
    }
    h = hashlib.sha256()

    if models_dir and models_dir.exists():
        for item in sorted(models_dir.glob("*.joblib")):
            fname = item.name
            file_hash = get_file_sha256(item)
            if fname in ("signal_model_long.joblib", "signal_model_short.joblib"):
                manifest["base_models"][fname] = file_hash
            elif fname.startswith("specialist_"):
                manifest["specialist_models"][fname] = file_hash
            elif "meta" in fname:
                manifest["meta_label_models"][fname] = file_hash
            else:
                manifest["base_models"][fname] = file_hash
            h.update(f"{fname}:{file_hash}".encode())

    # Fingerprint policy and challenger configuration
    h.update(json.dumps(active_policy or {}, sort_keys=True).encode())
    h.update(json.dumps(challenger_policies or [], sort_keys=True).encode())

    decision_artifacts_sha = h.hexdigest()
    return manifest, decision_artifacts_sha


def _downsample_curve(curve: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    if len(curve) <= MAX_CURVE_POINTS:
        return curve
    step = len(curve) / MAX_CURVE_POINTS
    sampled = [curve[int(i * step)] for i in range(MAX_CURVE_POINTS)]
    if sampled[-1] != curve[-1]:
        sampled.append(curve[-1])
    return sampled


async def run_backtest(
    data: DataManager,
    model: SignalModel,
    params: BacktestParams,
    on_progress: Callable[[float], None] | None = None,
) -> BacktestResult:
    import copy
    import hashlib
    model_orig = model
    # Refresh specialist models from disk before deepcopying. This ensures
    # the backtest always uses the latest trained artifacts even when training
    # ran after the server was started (avoids NO_ARTIFACT for new specialists).
    if hasattr(model, "reload_specialists"):
        model.reload_specialists()
    model = copy.deepcopy(model)

    # --- FIX #69: Explicitly reset backtest_start_time on the clone to None
    # before setting it to params.start. This guarantees the clone always
    # starts from a known-clean state regardless of any stale value that may
    # have been on the live singleton at deepcopy time.
    model.backtest_start_time = None

    models_dir = getattr(model, "models_dir", None)
    sha_long = get_file_sha256(models_dir / "signal_model_long.joblib") if models_dir else "N/A"
    sha_short = get_file_sha256(models_dir / "signal_model_short.joblib") if models_dir else "N/A"
    sha_long_adap = get_file_sha256(models_dir / "signal_model_long_adaptive.joblib") if models_dir else "N/A"
    sha_short_adap = get_file_sha256(models_dir / "signal_model_short_adaptive.joblib") if models_dir else "N/A"
    
    applog.info("backtest", f"=== BACKTEST DIAGNOSTIC START ===")
    applog.info("backtest", f"Model ID: {id(model)} (Orig: {id(model_orig)})")
    applog.info("backtest", f"Model Meta Trained At: {model.meta.get('trained_at', 'N/A')}")
    applog.info("backtest", f"Adaptive Long Loaded: {model._adaptive_long is not None}")
    applog.info("backtest", f"Adaptive Short Loaded: {model._adaptive_short is not None}")
    applog.info("backtest", f"Base Long SHA256: {sha_long}")
    applog.info("backtest", f"Base Short SHA256: {sha_short}")
    applog.info("backtest", f"Adaptive Long SHA256: {sha_long_adap}")
    applog.info("backtest", f"Adaptive Short SHA256: {sha_short_adap}")
    applog.info("backtest", f"ML Gate Threshold: {params.risk.ml_confidence_threshold if params.risk else 'N/A'}")
    applog.info("backtest", f"Watchlist: {params.symbols}")
    applog.info("backtest", f"Start: {params.start.isoformat()}")
    applog.info("backtest", f"End: {params.end.isoformat()}")
    applog.info("backtest", f"Strategy params: {params.strategies}")
    applog.info("backtest", f"=================================")

    # --- FIX #67: Capture ALL live DataManager state that the backtest mutates
    # so every attribute is unconditionally restored in the finally block,
    # regardless of success, exception, or cancellation.
    #
    # Mutated attributes:
    #   data.provenance     - reset to {} by backtest, never restored before this fix
    #   data.cache_policy   - set to params.cache_policy, never restored before this fix
    #   data.history        - monkey-patched to custom_history, restored by prior fix
    #
    # Capturing original_ values here before any mutation guarantees the live
    # engine always resumes with its own data semantics after backtest completion.
    from app.data.base import CachePolicy
    original_provenance = dict(data.provenance)  # shallow copy is sufficient
    original_cache_policy = data.cache_policy
    original_history = data.history
    # Apply run-scoped backtest state
    data.provenance = {}
    data.cache_policy = params.cache_policy
    # fix(#129): removed stale data.original_history = original_history assignment
    # (it was a debug holdover; original_history is captured as a local variable above)
    current_backtest_ts = [params.start]
    daily_frames = {}

    async def custom_history(
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        prefer_cache: bool = True,
        cache_policy: str | None = None,
    ) -> pd.DataFrame:
        symbol = symbol.upper()
        if symbol in frames:
            now_ts = current_backtest_ts[0]
            
            start_ts = pd.Timestamp(start)
            if start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("UTC")
            else:
                start_ts = start_ts.tz_convert("UTC")

            end_ts = pd.Timestamp(end)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")
            else:
                end_ts = end_ts.tz_convert("UTC")

            now_ts_pd = pd.Timestamp(now_ts)
            if now_ts_pd.tzinfo is None:
                now_ts_pd = now_ts_pd.tz_localize("UTC")
            else:
                now_ts_pd = now_ts_pd.tz_convert("UTC")

            if timeframe == "1Day" and symbol in daily_frames:
                df_daily = daily_frames[symbol]
                df_prev = df_daily[df_daily.index < now_ts_pd.normalize()]
                
                df_today_1m = frames[symbol][(frames[symbol].index >= now_ts_pd.normalize()) & (frames[symbol].index <= now_ts_pd)]
                if not df_today_1m.empty:
                    today_bar = pd.DataFrame([{
                        "open": float(df_today_1m["open"].iloc[0]),
                        "high": float(df_today_1m["high"].max()),
                        "low": float(df_today_1m["low"].min()),
                        "close": float(df_today_1m["close"].iloc[-1]),
                        "volume": float(df_today_1m["volume"].sum())
                    }], index=[now_ts_pd.normalize()])
                    today_bar.index.name = "ts"
                    sliced = pd.concat([df_prev, today_bar])
                else:
                    sliced = df_prev
                
                sliced = sliced[sliced.index >= start_ts.normalize()]
                return sliced

            df = frames[symbol]
            sliced = df[(df.index >= start_ts) & (df.index <= end_ts) & (df.index <= now_ts_pd)]
            if sliced.empty:
                from app.data.base import empty_bars
                return empty_bars()
            
            from app.data.base import resample_bars
            resampled = resample_bars(sliced, timeframe)
            return resampled

        return await original_history(symbol, timeframe, start, end, prefer_cache, cache_policy)

    data.history = custom_history

    # All replay logic is wrapped in try/finally to guarantee ALL mutated
    # DataManager attributes are restored on success, exception, and cancellation.
    try:
        before_hash = None
        if params.cache_policy == CachePolicy.IMMUTABLE_CACHE_ONLY and getattr(params, "verify_dataset_immutability", False):
            before_hash = data.cache.compute_canonical_hash()

        from app.config import get_settings
        settings = get_settings()
        historical_news = None
        if settings.alpaca_api_key and settings.alpaca_api_secret:
            try:
                from alpaca.data.historical import NewsClient
                from alpaca.data.requests import NewsRequest
                
                news_client = NewsClient(settings.alpaca_api_key, settings.alpaca_api_secret)
                historical_news = []
                
                page_token = None
                start_fetch = params.start - timedelta(days=2)
                
                def _fetch_all_news():
                    token = None
                    items = []
                    while True:
                        req = NewsRequest(
                            start=start_fetch,
                            end=params.end,
                            limit=1000,
                            sort="asc",
                            page_token=token
                        )
                        resp = news_client.get_news(req)
                        news_list = []
                        if hasattr(resp, "data") and isinstance(resp.data, dict):
                            news_list = resp.data.get("news", [])
                        elif isinstance(resp, dict):
                            news_list = resp.get("news", [])
                        
                        if not news_list:
                            break
                        for item in news_list:
                            items.append(item.model_dump() if hasattr(item, "model_dump") else item.__dict__)
                        token = getattr(resp, "next_page_token", None)
                        if not token:
                            break
                    return items

                historical_news = await asyncio.to_thread(_fetch_all_news)
                applog.info("backtest", f"Pre-fetched {len(historical_news)} historical news items for EventCalendar.")
            except Exception as exc:
                applog.warning("backtest", f"Failed to pre-fetch historical news: {exc}")
                
        from app.core.calendar import EventCalendar
        calendar = EventCalendar(alpaca_client=None)
        if historical_news is not None:
            calendar._historical_news = historical_news
            async def custom_fetch_news(symbols, start, end, limit=20):
                from datetime import datetime, UTC
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                
                out = []
                for item in calendar._historical_news:
                    item_ts = item.get("created_at")
                    if isinstance(item_ts, str):
                        item_ts = datetime.fromisoformat(item_ts.replace("Z", "+00:00"))
                    elif isinstance(item_ts, datetime):
                        if item_ts.tzinfo is None:
                            item_ts = item_ts.replace(tzinfo=UTC)
                        else:
                            item_ts = item_ts.astimezone(UTC)
                    else:
                        continue
                    
                    if start_dt <= item_ts <= end_dt:
                        if symbols:
                            item_symbols = {s.upper() for s in item.get("symbols", [])}
                            query_symbols = {s.upper() for s in symbols}
                            if not item_symbols.intersection(query_symbols):
                                continue
                        out.append(item)
                out.sort(key=lambda x: x.get("created_at"), reverse=True)
                return out[:limit]
                
            calendar._fetch_news = custom_fetch_news

        clock = MarketClock(always_open=False)
        risk_cfg = params.risk or RiskConfig()
        position_peaks: dict[str, float] = {}
        position_troughs: dict[str, float] = {}
        position_initial_stops: dict[str, float | None] = {}
        shadow_signals_log: list[dict] = []
        counterfactuals_log: list[dict] = []
        warnings: list[str] = []

        # ---- load & precompute (vectorized, strictly causal indicators) ----
        symbols_to_load = [s.upper() for s in params.symbols]
        if "SPY" not in symbols_to_load:
            symbols_to_load.append("SPY")
        if "QQQ" not in symbols_to_load:
            symbols_to_load.append("QQQ")
        if params.use_dynamic_scanner:
            from app.core.scanner import CANDIDATE_POOL, DEFENSIVE_WATCHLIST
            symbols_to_load = sorted(list(set(symbols_to_load + CANDIDATE_POOL + DEFENSIVE_WATCHLIST)))

        frames: dict[str, pd.DataFrame] = {}
        sem = asyncio.Semaphore(5)  # Fetch up to 5 symbols concurrently to speed up loading without triggering rate limits

        async def _load_symbol(symbol: str) -> None:
            async with sem:
                try:
                    # 1. Validate/load the exact requested replay range. Warmup is
                    # optional cached context and must not redefine range completeness.
                    start_utc = pd.Timestamp(params.start)
                    if start_utc.tzinfo is None:
                        start_utc = start_utc.tz_localize("UTC")
                    else:
                        start_utc = start_utc.tz_convert("UTC")
                    end_utc = pd.Timestamp(params.end)
                    if end_utc.tzinfo is None:
                        end_utc = end_utc.tz_localize("UTC")
                    else:
                        end_utc = end_utc.tz_convert("UTC")

                    requested_df = await original_history(symbol, "1Min", params.start, params.end)
                    requested_provenance = data.provenance.get(symbol)
                    requested_df = requested_df[
                        (requested_df.index >= start_utc) & (requested_df.index <= end_utc)
                    ]
                    if not requested_df.empty:
                        start_fetch = params.start - timedelta(days=10)
                        warmup_df = data.cache.query(symbol, "1Min", start_fetch, params.start)
                        warmup_df = warmup_df[warmup_df.index < start_utc].tail(600)
                        combined_df = pd.concat([warmup_df, requested_df])
                        combined_df = combined_df[~combined_df.index.duplicated(keep="last")].sort_index()
                        frames[symbol] = compute_features(combined_df)
                    elif symbol in [s.upper() for s in params.symbols]:
                        warnings.append(f"{symbol}: insufficient data -- skipped")
                        applog.warning("backtest", warnings[-1])

                    # 2. Daily history is optional scanner context. Read only available
                    # cached context so it cannot overwrite requested-range provenance or
                    # add provider calls to a replay.
                    start_fetch_daily = params.start - timedelta(days=40)
                    df_daily = data.cache.query(symbol, "1Day", start_fetch_daily, params.end)
                    if not df_daily.empty:
                        daily_frames[symbol] = df_daily[df_daily.index <= end_utc]
                    if requested_provenance is not None:
                        data.provenance[symbol] = requested_provenance

                except HistoricalDataCoverageError:
                    if symbol in [s.upper() for s in params.symbols]:
                        raise
                except Exception as exc:
                    if symbol in [s.upper() for s in params.symbols]:
                        warnings.append(f"{symbol}: download failed ({exc}) -- skipped")
                        applog.warning("backtest", warnings[-1])

        await asyncio.gather(*(_load_symbol(s) for s in symbols_to_load))

        if not frames:
            raise ValueError("No usable data for any requested symbol/date range")

        # Only resample as a fallback if not loaded in daily_frames
        from app.data.base import resample_bars
        for symbol, df in frames.items():
            if symbol not in daily_frames:
                daily_frames[symbol] = resample_bars(df, "1Day")

        # Initialize leakage-free SlotVolumeEngines for all loaded data
        slot_volume_engines = {
            symbol: SlotVolumeEngine(df, lookback_sessions=20, min_observations=5)
            for symbol, df in frames.items()
        }

        positions_index = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in frames.items()}
        all_ts = sorted(set().union(*[df.index for df in frames.values()]))

        # ---- assemble the identical live stack ----
        broker = PaperBroker(
            starting_cash=params.starting_cash,
            slippage_bps=params.slippage_bps,
            fill_market_on="next_open",
        )
        portfolio = PortfolioTracker()
        risk = RiskManager(risk_cfg, clock)
        core_watchlist = [s.upper() for s in params.symbols]
        current_watchlist = list(core_watchlist)
        risk.core_watchlist = core_watchlist
        # Set backtest_start_time on the replay clone to enforce temporal leakage
        # protection, unless explicitly bypassed by ignore_temporal_leakage.
        if params.ignore_temporal_leakage:
            model.backtest_start_time = None
        else:
            model.backtest_start_time = params.start
        pipeline = SignalPipeline(model, risk_cfg.ml_confidence_threshold if params.use_ml_gate else 0.0)
        # Set backtest_start_time on meta label engine to prevent MetaLabel temporal leakage
        if hasattr(pipeline, "meta_label_engine") and pipeline.meta_label_engine is not None:
            if params.ignore_temporal_leakage:
                pipeline.meta_label_engine.backtest_start_time = None
            else:
                pipeline.meta_label_engine.backtest_start_time = params.start
        from app.strategies.evaluator import ParallelEvidenceEvaluator
        evidence_evaluator = ParallelEvidenceEvaluator(
            model=model,
            ml_threshold=risk_cfg.ml_confidence_threshold if params.use_ml_gate else 0.0,
            meta_label_engine=pipeline.meta_label_engine,
            trade_score_engine=pipeline.trade_score_engine,
        )
        strategies = create_strategies(params.strategies)
        for s in strategies:
            s.reset()
        active = [s for s in strategies if s.enabled]
        trailing_exits_log = []
        last_stop_prices = {}

        from app.portfolio.allocator import PortfolioAIGate, CapitalAllocationOptimizer
        portfolio_gate = PortfolioAIGate(
            max_correlation=risk_cfg.max_correlation_penalty,
            max_positions_per_sector=risk_cfg.max_positions_per_sector
        )
        portfolio_allocator = CapitalAllocationOptimizer(
            kelly_fraction_cap=risk_cfg.kelly_fraction_cap
        )

        def account_ctx(now: datetime, pending_entries: list[str] | None = None) -> AccountContext:
            equity = broker.equity()
            return AccountContext(
                equity=equity,
                cash=broker.cash,
                positions=[p for p in broker._positions.values() if abs(p.qty) > 1e-9],
                day_pnl=portfolio.day_pnl(equity),
                session_start_equity=portfolio.session_start_equity or params.starting_cash,
                peak_equity=portfolio.peak_equity,
                day_trades_today=portfolio.day_trades_today,
                is_paper=True,
                pending_entry_symbols=pending_entries or [],
            )


        def drain(now: datetime) -> None:
            for fill in broker.drain_fills():
                symbol = fill.symbol.upper()
                peak = position_peaks.get(symbol)
                trough = position_troughs.get(symbol)
                init_stop = position_initial_stops.get(symbol)
                
                trade = portfolio.on_fill(fill, peak_price=peak, trough_price=trough, initial_stop=init_stop)
                if trade is not None:
                    # Log exact entry and exit timestamps for analysis
                    entry_ts_str = trade.entry_at.strftime("%Y-%m-%dT%H:%M:%S%z") if trade.entry_at else "N/A"
                    exit_ts_str  = trade.exit_at.strftime("%Y-%m-%dT%H:%M:%S%z")  if trade.exit_at  else "N/A"
                    applog.info(
                        "backtest",
                        f"[TRADE CLOSED] {trade.symbol} {trade.side.upper()} | "
                        f"ENTRY {entry_ts_str} @ ${trade.entry_price:.4f} | "
                        f"EXIT  {exit_ts_str}  @ ${trade.exit_price:.4f} | "
                        f"PnL=${trade.pnl:+.2f} R={trade.r_multiple:+.2f}R | "
                        f"reason={trade.exit_reason} strategy={trade.strategy}"
                    )
                    risk.on_trade_closed(trade, now=now)
                    
                    # Exit counterfactuals replay on completed trade
                    sym_frame = frames.get(symbol)
                    if sym_frame is not None and not sym_frame.empty:
                        try:
                            entry_ts = pd.Timestamp(trade.entry_at)

                            if sym_frame.index.tz is not None:
                                if entry_ts.tzinfo is None:
                                    entry_ts = entry_ts.tz_localize("UTC")
                                entry_ts = entry_ts.tz_convert(sym_frame.index.tz)
                            elif entry_ts.tzinfo is not None:
                                entry_ts = entry_ts.tz_localize(None)

                            entry_idx = sym_frame.index.searchsorted(entry_ts, side="right") - 1

                            if entry_idx < 0 or entry_idx >= len(sym_frame):
                                raise ValueError(
                                    f"Entry timestamp {trade.entry_at} outside available "
                                    f"counterfactual frame for {symbol}"
                                )
                            entry_price = trade.entry_price
                            side = trade.side
                            init_stop_price = init_stop if init_stop is not None else entry_price * (0.985 if side == "long" else 1.015)
                            entry_bar = sym_frame.iloc[entry_idx]
                            initial_atr = float(entry_bar["atr14"]) if "atr14" in entry_bar and pd.notna(entry_bar["atr14"]) else (entry_price * 0.01)
                            
                            counterfactuals = ExitCounterfactualEngine.replay_all_policies(
                                df=sym_frame,
                                entry_idx=entry_idx,
                                entry_price=entry_price,
                                side=side,
                                initial_stop=init_stop_price,
                                initial_atr=initial_atr,
                            )
                            counterfactuals_log.append({
                                "trade_id": trade.id,
                                "symbol": symbol,
                                "entry_at": trade.entry_at.isoformat(),
                                "exit_at": trade.exit_at.isoformat(),
                                "strategy": trade.strategy,
                                "direction": trade.side,
                                "entry_price": trade.entry_price,
                                "actual_exit_price": trade.exit_price,
                                "actual_r": trade.r_multiple,
                                "actual_pnl": trade.pnl,
                                "policies": counterfactuals,
                            })
                        except Exception:
                            pass
                    
                    # Cleanup peak/trough/stop
                    if symbol in position_peaks:
                        del position_peaks[symbol]
                    if symbol in position_troughs:
                        del position_troughs[symbol]
                    if symbol in position_initial_stops:
                        del position_initial_stops[symbol]
                    
                    # Record trailing stop exit if applicable
                    if trade.exit_reason == "stop_loss":
                        strat_obj = next((s for s in strategies if s.name == trade.strategy), None)
                        ts_mult = strat_obj.params.get("trailing_stop_atr_mult", 0.0) if strat_obj else 0.0
                        if ts_mult > 0.0:
                            last_stop = last_stop_prices.get(trade.symbol)
                            if last_stop is not None and init_stop is not None and last_stop != init_stop:
                                trade_risk = abs(trade.entry_price - init_stop)
                                mfe_dollars = abs(peak - trade.entry_price) if peak else 0.0
                                mfe_r = mfe_dollars / trade_risk if trade_risk > 0 else 0.0
                                
                                trailing_exits_log.append({
                                    "timestamp": trade.exit_at.isoformat(),
                                    "symbol": trade.symbol,
                                    "strategy": trade.strategy,
                                    "direction": trade.side,
                                    "entry_price": trade.entry_price,
                                    "best_price": peak,
                                    "mfe_r": round(mfe_r, 2),
                                    "trailing_multiplier": ts_mult,
                                    "stop_price": last_stop,
                                    "exit_price": trade.exit_price,
                                    "final_r": round(trade.r_multiple, 2) if trade.r_multiple else 0.0,
                                    "initial_stop": init_stop,
                                    "entry_at": trade.entry_at.isoformat()
                                })
                else:
                    pos = broker._positions.get(fill.symbol)
                    if pos is not None:
                        portfolio.note_stop(fill.symbol, pos.stop_loss)
                        # Log exact entry fill timestamp for analysis
                        entry_ts_str = fill.ts.strftime("%Y-%m-%dT%H:%M:%S%z") if fill.ts else now.strftime("%Y-%m-%dT%H:%M:%S%z")
                        applog.info(
                            "backtest",
                            f"[TRADE OPENED] {fill.symbol} {fill.side.upper()} | "
                            f"ENTRY {entry_ts_str} @ ${fill.price:.4f} qty={fill.qty} | "
                            f"stop={pos.stop_loss:.4f} strategy={fill.strategy}"
                        )

        # ---- event loop ----
        bars_processed = 0
        total = len(all_ts)
        flattened_sessions: set = set()
        last_scan_date = None
        last_event_monitor_min = None
        last_event_calendar_min = None
        last_intraday_scan_min = None
        scanned_watchlist = []

        start_utc = pd.Timestamp(params.start)
        if start_utc.tzinfo is None:
            start_utc = start_utc.tz_localize("UTC")
        else:
            start_utc = start_utc.tz_convert("UTC")

        for step, ts in enumerate(all_ts):
            current_backtest_ts[0] = ts
            if ts < start_utc:
                continue
            now = (ts + timedelta(minutes=1)).to_pydatetime()
            session = clock.session_date(now)

            # Daily Morning Scan & Correlation Matrix Update (at 9:00 AM ET)
            # F-15 FIX: ZoneInfo import moved to module level (was imported per-bar in the hot loop).
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            today_date = now_et.date()
            et_mins = now_et.hour * 60 + now_et.minute
            
            if last_scan_date != today_date and et_mins >= 9 * 60:
                last_scan_date = today_date
                
                if params.use_dynamic_scanner:
                    applog.info("backtest", f"[{now}] Running historical pre-market scanner...")
                    from app.core.scanner import MarketScanner
                    scanner = MarketScanner(data, core_watchlist)
                    try:
                        from app.core.scanner import ScannerOutcome
                        outcome, scanned = await scanner.run(as_of=now)
                        
                        if outcome == ScannerOutcome.SUCCESS_NONEMPTY:
                            # Replace dynamic scanner watchlist with new symbols
                            open_symbols = {p.symbol.upper() for p in broker._positions.values() if abs(p.qty) > 1e-9}
                            protected = open_symbols & set(scanned_watchlist)
                            scanned_watchlist = sorted(list(set(scanned) | protected))
                            new_watchlist = sorted(list(set(core_watchlist + scanned_watchlist)))
                            if new_watchlist != current_watchlist:
                                current_watchlist = new_watchlist
                                risk.core_watchlist = list(current_watchlist)
                                applog.info("backtest", f"Watchlist dynamically rotated to: {current_watchlist}")
                                
                        elif outcome == ScannerOutcome.SUCCESS_EMPTY:
                            # Clear dynamic scanner watchlist, retain core
                            scanned_watchlist = []
                            new_watchlist = sorted(list(set(core_watchlist)))
                            if new_watchlist != current_watchlist:
                                current_watchlist = new_watchlist
                                risk.core_watchlist = list(current_watchlist)
                                applog.info("backtest", f"Watchlist dynamically rotated to: {current_watchlist}")
                                
                        elif outcome == ScannerOutcome.FAILED:
                            # Retain last-known-good dynamic watchlist + merge with core
                            applog.warning("backtest", "Scanner FAILED; retaining last-known-good dynamic candidates.")
                            new_watchlist = sorted(list(set(core_watchlist + scanned_watchlist)))
                            if new_watchlist != current_watchlist:
                                current_watchlist = new_watchlist
                                risk.core_watchlist = list(current_watchlist)
                                applog.info("backtest", f"Watchlist retained on failure: {current_watchlist}")
                        
                        # Targeted causal training: fetch data for scanned tickers up to now (9:00 AM ET)
                        applog.info("backtest", "Pre-market scan complete. Starting backtest in-memory targeted adaptive training...")
                        end_train = now
                        start_train = end_train - timedelta(days=30)
                        train_frames = {}
                        for symbol in scanned:
                            if symbol in frames:
                                sym_i = positions_index[symbol].get(ts)
                                if sym_i is not None:
                                    df = await data.history(symbol, "1Min", start_train, end_train)
                                    if not df.empty:
                                        train_frames[symbol] = df
                        if train_frames:
                            model.train_adaptive(train_frames, horizon_bars=15, threshold_pct=0.10, save_to_disk=False)
                            applog.info("backtest", "Backtest targeted daily adaptive training completed in-memory.")
                        else:
                            applog.warning("backtest", "No historical data for daily adaptive training.")
                    except Exception as exc:
                        applog.warning("backtest", f"Historical scanner/adaptive training failed: {exc}. Retaining last-known-good candidates.")
                        new_watchlist = sorted(list(set(core_watchlist + scanned_watchlist)))
                        if new_watchlist != current_watchlist:
                            current_watchlist = new_watchlist
                            risk.core_watchlist = list(current_watchlist)
                
                # Recompute correlation matrix daily for the current active watchlist
                corr_frames = {}
                for sym in current_watchlist:
                    if sym in frames:
                        sym_i = positions_index[sym].get(ts)
                        if sym_i is not None:
                            start_idx = max(0, sym_i - 12000 + 1)
                            corr_frames[sym] = frames[sym].iloc[start_idx : sym_i + 1]
                try:
                    portfolio_gate.update_correlation_matrix(corr_frames)
                except Exception as exc:
                    applog.warning("backtest", f"Failed to update portfolio correlation matrix: {exc}")

            # Intraday Watchlist updates (only if dynamic scanner is enabled)
            if params.use_dynamic_scanner:
                # Intraday Watchlist Refresh (9:30 AM - 3:30 PM ET, every 30 minutes)
                if (
                    9 * 60 + 30 <= et_mins <= 15 * 60 + 30
                    and et_mins % 30 == 0
                    and last_intraday_scan_min != et_mins
                ):
                    last_intraday_scan_min = et_mins
                    from app.core.scanner import MarketScanner, ScannerOutcome
                    scanner = MarketScanner(data, core_watchlist)
                    try:
                        outcome, scanned_result = await scanner.run(as_of=now)
                        
                        if outcome == ScannerOutcome.SUCCESS_NONEMPTY:
                            open_symbols = {p.symbol.upper() for p in broker._positions.values() if abs(p.qty) > 1e-9}
                            existing = set(scanned_watchlist)
                            new_set = set(scanned_result)
                            to_remove = (existing - new_set) - open_symbols
                            to_add = new_set - existing
                            scanned_watchlist = sorted(list((existing - to_remove) | to_add))
                            new_watchlist = sorted(list(set(core_watchlist + scanned_watchlist)))
                            if new_watchlist != current_watchlist:
                                current_watchlist = new_watchlist
                                risk.core_watchlist = list(current_watchlist)
                                applog.info("backtest", f"Intraday watchlist refresh: +{list(to_add)} -{list(to_remove)}")
                        
                        elif outcome == ScannerOutcome.SUCCESS_EMPTY:
                            open_symbols = {p.symbol.upper() for p in broker._positions.values() if abs(p.qty) > 1e-9}
                            existing = set(scanned_watchlist)
                            to_remove = existing - open_symbols
                            scanned_watchlist = sorted(list(existing - to_remove))
                            new_watchlist = sorted(list(set(core_watchlist + scanned_watchlist)))
                            if new_watchlist != current_watchlist:
                                current_watchlist = new_watchlist
                                risk.core_watchlist = list(current_watchlist)
                                applog.info("backtest", f"Intraday watchlist refresh: +[] -{list(to_remove)}")
                                
                        elif outcome == ScannerOutcome.FAILED:
                            applog.warning("backtest", "Intraday scanner FAILED; retaining last-known-good dynamic candidates.")
                            
                    except Exception as exc:
                        applog.warning("backtest", f"Intraday scanner refresh failed: {exc}")

                # Intraday Event Monitor (9:30 AM to 4:00 PM ET, every 5 minutes)
                if 9 * 60 + 30 <= et_mins <= 16 * 60 and et_mins % 5 == 0 and last_event_monitor_min != et_mins:
                    last_event_monitor_min = et_mins
                    from app.core.scanner import CANDIDATE_POOL
                    from app.indicators.ta import enrich
                    non_watchlist = [s for s in CANDIDATE_POOL if s not in current_watchlist]
                    if non_watchlist:
                        start_time = now - timedelta(minutes=60)
                        for symbol in non_watchlist:
                            try:
                                df = await data.history(symbol, "1Min", start_time, now)
                                if df.empty or len(df) < 15:
                                    continue
                                close_now = float(df["close"].iloc[-1])
                                open_5m = float(df["open"].iloc[-5])
                                ret_5m = abs(close_now / open_5m - 1.0) * 100.0
                                
                                vol_5m = float(df["volume"].iloc[-5:].sum())
                                avg_vol_1m = float(df["volume"].mean())
                                avg_vol_5m = avg_vol_1m * 5.0
                                rvol = vol_5m / avg_vol_5m if avg_vol_5m > 0 else 0.0
                                
                                if ret_5m >= 1.5 or rvol >= 3.0:
                                    df_enriched = enrich(df)
                                    row = df_enriched.iloc[-1]
                                    p_up = model.score_action(row, SignalAction.ENTER_LONG, symbol)
                                    p_down = model.score_action(row, SignalAction.ENTER_SHORT, symbol)
                                    max_prob = max(p_up, p_down)
                                    
                                    if max_prob >= 0.55:
                                        if symbol not in scanned_watchlist:
                                            scanned_watchlist.append(symbol)
                                            current_watchlist = sorted(list(set(core_watchlist + scanned_watchlist)))
                                            risk.core_watchlist = list(current_watchlist)
                                            applog.info(
                                                "backtest",
                                                f"[{now}] [Event Monitor] Added symbol {symbol} dynamically."
                                            )
                            except Exception as _exc:
                                applog.debug(
                                    "backtest",
                                    f"[{now}] [Event Monitor] {symbol} skipped - transient error: {type(_exc).__name__}: {_exc}",
                                )

            # Check event calendar every 5 minutes (aligned with live event monitor)
            if 9 * 60 + 30 <= et_mins <= 16 * 60 and et_mins % 5 == 0 and last_event_calendar_min != et_mins:
                last_event_calendar_min = et_mins
                try:
                    is_event, reason = await calendar.check(now, current_watchlist)
                    pipeline.set_event_day(is_event)
                    if is_event:
                        applog.info("backtest", f"[{now}] Event guard ACTIVE - score threshold raised to event level ({reason[:60]})")
                except Exception as exc:
                    applog.debug("backtest", f"Calendar check error: {exc}")

            symbols_here = [s for s, idx in positions_index.items() if ts in idx]
            for symbol in symbols_here:
                i = positions_index[symbol][ts]
                broker.process_bar(symbol, frames[symbol].iloc[i], now)
                bars_processed += 1
            drain(now)

            if portfolio.roll_session(session, broker.equity()):
                risk.roll_session(session)

            # Update midday filter status (aligned with live engine)
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            et_mins = now_et.hour * 60 + now_et.minute
            is_midday = risk_cfg.midday_start_et <= et_mins <= risk_cfg.midday_end_et
            pipeline.set_midday_active(is_midday)

            ctx = account_ctx(now)
            halt = risk.check_halts(ctx, now=now)
            if halt:
                await broker.flatten_all(reason=f"{halt} halt")
                drain(now)
            elif risk.should_flatten(now) and session not in flattened_sessions:
                flattened_sessions.add(session)
                await broker.flatten_all(reason="end of day")
                drain(now)
                # Compile EOD policy reflection report and auto-promote if qualified (CR-146 Phase 7)
                try:
                    from app.strategies import generate_eod_reflection_report
                    generate_eod_reflection_report(params.db if hasattr(params, "db") else None, now=now, engine=None)
                except Exception as _exc:
                    applog.debug("backtest", f"Backtest EOD reflection skipped: {_exc}")
            elif not risk.hard_halted and not risk.daily_halted:
                candidates_this_tick = []
                for symbol in symbols_here:
                    if params.use_dynamic_scanner and symbol not in current_watchlist:
                        continue
                    i = positions_index[symbol][ts]
                    frame = frames[symbol]
                    window = frame.iloc[max(0, i - SLICE_WINDOW + 1) : i + 1]
                    position = broker._positions.get(symbol)

                    if position is not None:
                        close = float(window["close"].iloc[-1])
                        high_val = float(window["high"].iloc[-1])
                        low_val = float(window["low"].iloc[-1])
                        if symbol not in position_peaks:
                            position_peaks[symbol] = position.avg_entry_price
                            position_troughs[symbol] = position.avg_entry_price
                            position_initial_stops[symbol] = position.stop_loss
                            last_stop_prices[symbol] = position.stop_loss
                        if position.qty > 0:
                            position_peaks[symbol] = max(position_peaks[symbol], high_val)
                            position_troughs[symbol] = min(position_troughs[symbol], low_val)
                        else:
                            position_peaks[symbol] = min(position_peaks[symbol], low_val)
                            position_troughs[symbol] = max(position_troughs[symbol], high_val)

                        strat_obj = next((s for s in strategies if s.name == position.strategy), None)
                        if strat_obj is not None:
                            ts_mult = strat_obj.params.get("trailing_stop_atr_mult")
                            if ts_mult is not None and ts_mult > 0:
                                atr = float(window["atr14"].iloc[-1]) if pd.notna(window.get("atr14").iloc[-1]) else 0.0
                                if atr > 0:
                                    triggered = False
                                    stop_price = 0.0
                                    if position.qty > 0:
                                        stop_price = position_peaks[symbol] - ts_mult * atr
                                        if close <= stop_price:
                                            triggered = True
                                    else:
                                        stop_price = position_peaks[symbol] + ts_mult * atr
                                        if close >= stop_price:
                                            triggered = True

                                    last_stop_prices[symbol] = stop_price

                                    if triggered:
                                        applog.info("backtest", f"{symbol}: trailing stop triggered @ {close:.2f} (stop {stop_price:.2f})")
                                        await broker.close_position(symbol, reason=f"trailing stop triggered ({ts_mult}x ATR)")
                                        if symbol in position_peaks:
                                            del position_peaks[symbol]
                                        if symbol in position_troughs:
                                            del position_troughs[symbol]
                                        if symbol in position_initial_stops:
                                            del position_initial_stops[symbol]
                                        drain(now)
                                        continue
                    else:
                        if symbol in position_peaks:
                            del position_peaks[symbol]
                        if symbol in position_troughs:
                            del position_troughs[symbol]
                        if symbol in position_initial_stops:
                            del position_initial_stops[symbol]

                    rs_score = None
                    spy_df = frames.get("SPY")
                    if spy_df is not None:
                        spy_i = positions_index["SPY"].get(ts)
                        if spy_i is not None:
                            spy_window = spy_df.iloc[max(0, spy_i - SLICE_WINDOW + 1) : spy_i + 1]
                            rs_score = compute_relative_strength(window, spy_window)

                    regime, _ = detect_regime(window)
                    sctx = StrategyContext(position=position, equity=broker.equity(), regime=regime, now=now, debug_mode=params.debug_mode)
                    signals = []
                    for strat in active:
                        try:
                            sig = strat.on_bar(sctx, symbol, window)
                        except Exception as exc:  # noqa: BLE001
                            applog.error("backtest", f"{strat.name} failed on {symbol}: {exc}")
                            continue
                        if sig is not None:
                            signals.append(sig)
                    if not signals:
                        continue
                    feature_row = window.iloc[-1]
                    slot_engine = slot_volume_engines.get(symbol)
                    slot_rvol_val = slot_engine.get_slot_rvol(now) if slot_engine else None

                    # Estimate SPY 30-bar rolling volatility
                    spy_vol: float | None = None
                    if spy_df is not None:
                        spy_i = positions_index["SPY"].get(ts)
                        if spy_i is not None:
                            spy_window = spy_df.iloc[max(0, spy_i - SLICE_WINDOW + 1) : spy_i + 1]
                            try:
                                spy_vol = float(spy_window["close"].pct_change().rolling(30).std().iloc[-1] * 100)
                            except Exception:
                                spy_vol = None

                    # Fetch Level 1 Context expectancy for each strategy and log/adapt (CR-146 Phase 5)
                    from app.strategies import get_contextual_expectancy, get_adaptive_ml_threshold, get_adaptive_signal_score
                    from app.core.phase import classify_clock_phase
                    clock_phase = classify_clock_phase(now)
                    adaptive_ml_thresholds = {}
                    for sig in signals:
                        if sig.action != SignalAction.EXIT:
                            try:
                                exp_info = get_contextual_expectancy(
                                    db=None,  # No DB in backtest engine
                                    strategy=sig.strategy,
                                    direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                                    regime=regime.name if hasattr(regime, "name") else str(regime),
                                    clock_phase=clock_phase.value if hasattr(clock_phase, "value") else str(clock_phase),
                                    rvol=slot_rvol_val,
                                    spy_vol=spy_vol,
                                )
                                applog.debug(
                                    "backtest.memory",
                                    f"Context memory lookup for {sig.symbol} {sig.strategy} {sig.action}: "
                                    f"expectancy={exp_info['expectancy']}, shrunk_expectancy={exp_info['shrunk_expectancy']}"
                                )
                                
                                dyn_ml_threshold = get_adaptive_ml_threshold(pipeline.ml_threshold, exp_info)
                                adaptive_ml_thresholds[sig.candidate_id] = dyn_ml_threshold
                                
                                if sig.score is not None:
                                    sig.score = get_adaptive_signal_score(sig.score, exp_info)
                            except Exception as exc:
                                applog.error("backtest.memory", f"Failed memory expectancy lookup / adaptation: {exc}")
                    # Assign a unique candidate ID to all candidate entry signals first (shared UUID)
                    import uuid
                    for sig in signals:
                        if sig.action != SignalAction.EXIT:
                            if getattr(sig, "candidate_id", None) is None:
                                prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                                sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()

                    # Run ParallelEvidenceEvaluator out-of-band to capture the parallel evidence (CR-146)
                    parallel_evidences = {}
                    for sig in signals:
                        if sig.action != SignalAction.EXIT:
                            try:
                                midday_active = getattr(pipeline, "_midday_active", False)
                                is_event_day = getattr(pipeline, "_is_event_day", False)
                                evidence = evidence_evaluator.evaluate(
                                    symbol=symbol,
                                    df=window,
                                    sig=sig,
                                    regime=regime,
                                    sector_strength=rs_score,
                                    is_event_day=is_event_day,
                                    midday_active=midday_active,
                                    ml_threshold=adaptive_ml_thresholds.get(sig.candidate_id),
                                )
                                if evidence:
                                    parallel_evidences[sig.candidate_id] = evidence
                            except Exception as exc:
                                applog.error("backtest.evaluator", f"ParallelEvidenceEvaluator failed for {symbol} {sig.strategy}: {exc}")

                    accepted, rejected = pipeline.process(symbol, window, signals, regime, features=feature_row, sector_strength=rs_score)

                    spy_df = frames.get("SPY")

                    for sig in signals:
                        if sig.action == SignalAction.EXIT:
                            continue

                        is_accepted = sig in accepted
                        final_decision = "accepted" if is_accepted else "rejected"
                        
                        rejection_stage = "none"
                        rejection_reason = ""
                        if not is_accepted:
                            rejection_reason = "rejected"
                            for rsig, rreason in rejected:
                                if rsig.strategy == sig.strategy and rsig.action == sig.action:
                                    rejection_reason = rreason
                                    break
                            if "ML confidence" in rejection_reason:
                                rejection_stage = "ml_gate"
                            elif "Specialist model validation" in rejection_reason:
                                rejection_stage = "specialist_validation"
                            elif "Meta-label" in rejection_reason:
                                rejection_stage = "metalabel_gate"
                            elif "Trade Score" in rejection_reason:
                                rejection_stage = "trade_score_gate"
                            elif "lost conflict" in rejection_reason:
                                rejection_stage = "conflict_resolution"
                            else:
                                rejection_stage = "other"

                        if is_accepted:
                            ml_prob_str = f"{sig.ml_probability:.2%}" if sig.ml_probability is not None else "N/A"
                            score_str = f"{sig.score:.2f}" if sig.score is not None else "N/A"
                            applog.info(
                                "backtest",
                                f"[{now}] Signal ACCEPTED by pipeline: {sig.symbol} {sig.action} "
                                f"(strategy: {sig.strategy}, score: {score_str}, ML prob: {ml_prob_str})"
                            )
                        else:
                            applog.info(
                                "backtest",
                                f"[{now}] Signal REJECTED by pipeline: {sig.symbol} {sig.action} "
                                f"(strategy: {sig.strategy}) -- Stage: {rejection_stage}, Reason: {rejection_reason}"
                            )

                        vwap_tel = compute_vwap_telemetry(window, now, slot_rvol_val)

                        payload = ShadowLedger.record_candidate(
                            db_engine=None,
                            symbol=symbol,
                            ts=now,
                            strategy=sig.strategy,
                            direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                            signal_price=sig.price or float(window["close"].iloc[-1]),
                            stop_loss=sig.stop_loss,
                            take_profit=sig.take_profit,
                            ml_confidence=sig.ml_probability,
                            final_decision=final_decision,
                            rejection_stage=rejection_stage,
                            rejection_reason=rejection_reason,
                            df=frame,
                            spy_df=spy_df,
                            slot_rvol_val=slot_rvol_val,
                            vwap_telemetry=vwap_tel,
                            mode="backtest",
                            candidate_id=sig.candidate_id,
                            strategy_params=getattr(sig, "strategy_params", {}),
                            policy_id="champion",
                            parent_policy_id=None,
                            policy_version="1.0.0",
                            gate_evidence_json=parallel_evidences.get(sig.candidate_id),
                            spy_vol=spy_vol,
                        )
                        shadow_signals_log.append(payload)

                        # Run and record all active Challengers in backtesting (CR-146 Phase 3)
                        from app.strategies.challenger import get_active_challengers
                        for challenger in get_active_challengers():
                            try:
                                challenger_eval = ParallelEvidenceEvaluator(
                                    model=model,
                                    ml_threshold=challenger.ml_threshold,
                                    meta_label_engine=pipeline.meta_label_engine,
                                    trade_score_engine=challenger.create_custom_trade_score_engine(pipeline.trade_score_engine),
                                )
                                c_evidence = challenger_eval.evaluate(
                                    symbol=symbol,
                                    df=window,
                                    sig=sig,
                                    regime=regime,
                                    sector_strength=rs_score,
                                    is_event_day=is_event_day,
                                    midday_active=midday_active,
                                )
                                
                                c_vetoes = c_evidence.get("vetoes", {})
                                c_accepted = len(c_vetoes) == 0
                                c_final_decision = "pipeline_approved" if c_accepted else "rejected"
                                
                                c_rejection_stage = "none"
                                c_rejection_reason = ""
                                if not c_accepted:
                                    stage = list(c_vetoes.keys())[0]
                                    c_rejection_stage = stage
                                    c_rejection_reason = c_vetoes[stage]

                                c_payload = ShadowLedger.record_candidate(
                                    db_engine=None,
                                    symbol=symbol,
                                    ts=now,
                                    strategy=sig.strategy,
                                    direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                                    signal_price=sig.price or float(window["close"].iloc[-1]),
                                    stop_loss=sig.stop_loss,
                                    take_profit=sig.take_profit,
                                    ml_confidence=sig.ml_probability,
                                    final_decision=c_final_decision,
                                    rejection_stage=c_rejection_stage,
                                    rejection_reason=c_rejection_reason,
                                    df=frame,
                                    spy_df=spy_df,
                                    slot_rvol_val=slot_rvol_val,
                                    vwap_telemetry=vwap_tel,
                                    mode="challenger",
                                    candidate_id=sig.candidate_id,
                                    strategy_params=getattr(sig, "strategy_params", {}),
                                    policy_id=challenger.policy_id,
                                    parent_policy_id="champion",
                                    policy_version="1.0.0",
                                    gate_evidence_json=c_evidence,
                                    spy_vol=spy_vol,
                                )
                                shadow_signals_log.append(c_payload)
                            except Exception as exc:
                                applog.error("backtest.challenger", f"Challenger {challenger.policy_id} failed for {symbol}: {exc}")

                    for sig in accepted:
                        if sig.action == SignalAction.EXIT:
                            if position is not None:
                                await broker.close_position(symbol, reason=sig.reason)
                        else:
                            candidates_this_tick.append((sig, window))

                if candidates_this_tick:
                    from app.portfolio.allocator import process_portfolio_allocations
                    from app.risk.manager import is_entry_order

                    alloc_signals = [sig for sig, _ in candidates_this_tick]
                    window_by_symbol = {sig.symbol: win for sig, win in candidates_this_tick}
                    open_positions = [p for p in broker._positions.values() if abs(p.qty) > 1e-9]

                    decision_outcome = process_portfolio_allocations(
                        signals=alloc_signals,
                        positions=open_positions,
                        available_capital=broker.cash,
                        equity=broker.equity(),
                        portfolio_gate=portfolio_gate,
                        portfolio_allocator=portfolio_allocator,
                    )

                    for sig, reason in decision_outcome.gated_entries:
                        applog.info(
                            "backtest",
                            f"[{now}] {sig.symbol}: Portfolio Gate/Allocator rejected -- {reason}"
                        )

                    pending_entries = [
                        broker._orders[oid].symbol.upper() for oid in broker._resting
                        if is_entry_order(broker._orders[oid], open_positions)
                    ]
                    effective_pending_entries = set(pending_entries)

                    for sig, kelly_val in decision_outcome.approved_entries:
                        win = window_by_symbol[sig.symbol]
                        ctx = account_ctx(now, pending_entries=list(effective_pending_entries))
                        decision = risk.evaluate_entry(sig, ctx, win, now=now, kelly_override=kelly_val)
                        if decision.approved and decision.order is not None:
                            position_initial_stops[sig.symbol] = decision.order.stop_loss
                            order = await broker.submit(decision.order)
                            
                            if order is not None and order.status not in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                                effective_pending_entries.add(sig.symbol.upper())
                        else:
                            applog.info(
                                "backtest",
                                f"[{now}] {sig.symbol}: RiskManager rejected entry -- {decision.reason}"
                            )

                drain(now)

            portfolio.mark_equity(broker.equity(), now)
            if on_progress and total and step % 200 == 0:
                on_progress(step / total)

        # ---- wrap up ----
        final_now = (all_ts[-1] + timedelta(minutes=2)).to_pydatetime()
        await broker.flatten_all(reason="backtest end")
        drain(final_now)
        portfolio.mark_equity(broker.equity(), final_now)

        # Immutability validation
        if before_hash is not None:
            after_hash = data.cache.compute_canonical_hash()
            if before_hash != after_hash:
                raise ValueError(
                    f"HISTORICAL_DATASET_MUTATED_DURING_RUN: Cache db changed. "
                    f"Before: {before_hash}, After: {after_hash}"
                )

        # Calculate actual ranges and session counts
        actual_data_start = all_ts[0].to_pydatetime() if all_ts else None
        actual_data_end = all_ts[-1].to_pydatetime() if all_ts else None
        
        ET_TZ = ZoneInfo("America/New_York")
        curr_d = params.start.astimezone(ET_TZ).date()
        end_d = params.end.astimezone(ET_TZ).date()
        requested_session_count = 0
        while curr_d <= end_d:
            if clock.is_trading_day(curr_d):
                requested_session_count += 1
            curr_d += timedelta(days=1)
            
        actual_session_dates = sorted(list(set(clock.session_date(ts) for ts in all_ts)))
        actual_session_count = len(actual_session_dates)
        
        if requested_session_count != actual_session_count:
            warnings.append(
                f"Session count mismatch: requested {requested_session_count} but "
                f"processed {actual_session_count} actual sessions."
            )

        symbol_session_dates = []
        for sym, frame in frames.items():
            sym_dates = set(clock.session_date(ts) for ts in frame.index)
            symbol_session_dates.append(sym_dates)
        shared_session_count = len(set.intersection(*symbol_session_dates)) if symbol_session_dates else 0

        sources = set(p.get("source") for p in data.provenance.values())
        if len(sources) == 1:
            data_source_mode = "CACHE_ONLY" if "cache" in sources else "NETWORK_ONLY"
        elif len(sources) > 1:
            data_source_mode = "MIXED"
        else:
            data_source_mode = "CACHE_ONLY"

        config_fields = {
            "symbols": sorted(params.symbols),
            "start": params.start.strftime("%Y-%m-%d"),
            "end": params.end.strftime("%Y-%m-%d"),
            "starting_cash": params.starting_cash,
            "slippage_bps": params.slippage_bps,
            "use_ml_gate": params.use_ml_gate,
            "ml_confidence_threshold": risk_cfg.ml_confidence_threshold,
            "risk_per_trade_pct": risk_cfg.risk_per_trade_pct,
            "max_open_positions": risk_cfg.max_open_positions,
        }
        import json
        config_sha = hashlib.sha256(json.dumps(config_fields, sort_keys=True).encode()).hexdigest()
        code_sha = compute_code_state_sha256()
        
        models_dir = getattr(model, "models_dir", None)
        model_sha = ""
        if models_dir:
            h = hashlib.sha256()
            for fname in sorted(["signal_model_long.joblib", "signal_model_short.joblib"]):
                p = models_dir / fname
                if p.exists():
                    h.update(get_file_sha256(p).encode())
                else:
                    h.update(f"MISSING:{fname}".encode())
            model_sha = h.hexdigest()

        # Issue #215: Comprehensive decision-artifact manifest and fingerprint
        active_policy_snap = getattr(risk, "active_policy_snapshot", None)
        active_challengers = getattr(risk, "challengers", None)
        artifacts_manifest, decision_artifacts_sha = compute_decision_artifacts_manifest(
            models_dir=models_dir,
            active_policy=active_policy_snap if isinstance(active_policy_snap, dict) else {},
            challenger_policies=active_challengers if isinstance(active_challengers, list) else [],
        )

        dataset_sha = ""
        if not data.is_synthetic:
            dataset_sha = data.cache.compute_canonical_hash()

        metrics = compute_metrics(portfolio.equity_curve, portfolio.trades, params.starting_cash)
        gate_value_report = build_gate_value_report(shadow_signals_log)
        applog.info(
            "backtest",
            f"Gate value ({gate_value_report['horizon']}): "
            f"accepted={gate_value_report['accepted']['candidates']} "
            f"accepted_expectancy_r={gate_value_report['accepted']['expectancy_r']} "
            f"rejected={gate_value_report['rejected']['candidates']} "
            f"rejected_expectancy_r={gate_value_report['rejected']['expectancy_r']}",
        )

        combined_provenance = dict(data.provenance) if data.provenance else {}
        combined_provenance["decision_artifacts_manifest"] = artifacts_manifest
        combined_provenance["model_eligibility_mode"] = params.model_eligibility_mode

        result = BacktestResult(
            params=params.model_dump(mode="json"),
            metrics=metrics,
            equity_curve=_downsample_curve(portfolio.equity_curve),
            trades=portfolio.trades,
            bars_processed=bars_processed,
            trailing_exits=trailing_exits_log,
            warnings=warnings,
            shadow_signals=shadow_signals_log,
            counterfactuals=counterfactuals_log,
            gate_value_report=gate_value_report,
            
            requested_start=params.start,
            requested_end=params.end,
            actual_data_start=actual_data_start,
            actual_data_end=actual_data_end,
            requested_session_count=requested_session_count,
            actual_session_count=actual_session_count,
            shared_session_count=shared_session_count,
            data_source_mode=data_source_mode,
            config_sha256=config_sha,
            code_state_sha256=code_sha,
            dataset_sha256=dataset_sha,
            model_sha256=model_sha,
            decision_artifacts_sha256=decision_artifacts_sha,
            provenance=combined_provenance,
        )
        
        if params.debug_mode:
            from app.core.benchmark import run_benchmark_attribution
            bars_dict = {}
            for sym in set(params.symbols + risk.core_watchlist):
                if sym in frames:
                    bars_dict[sym] = frames[sym]
            run_benchmark_attribution(result, bars_dict)

        applog.info(
            "backtest",
            f"Backtest done: {bars_processed} bars, {metrics['total_trades']} trades, "
            f"return {metrics['total_return_pct']}%, maxDD {metrics['max_drawdown_pct']}%",
        )
        return result

    finally:
        # --- FIX #67 / #69 / #129: Unconditionally restore ALL mutated DataManager
        # attributes on success, exception, and cancellation.
        # A failed or cancelled backtest can never leave the live DataManager
        # in a corrupted or history-patched state.
        data.history = original_history
        data.provenance = original_provenance
        data.cache_policy = original_cache_policy
        # Remove stale debug attribute if it was previously set by an older run
        if hasattr(data, "original_history"):
            del data.original_history
