"""MarketScanner -- parallel swarm scanner covering ~200 symbols across 7 market sectors.

Pipeline:
  Stage 1  Fast pre-filter  (asyncio.gather, semaphore=20)  ~200 symbols -> ~60
  Stage 2  Technical score  (instant, no extra API calls)   ~60 -> top 25
  Stage 3  ML AUC validate  (asyncio.gather, semaphore=8)   top 25 -> final 10-15

Safety gates:
  - Multi-signal MarketRiskState (CR-047): NORMAL/CAUTION/STRESS/PANIC/UNKNOWN
    aggregated from 5 independent signal families (SPY, QQQ, breadth,
    universe_stress, data_health)
  - UNKNOWN fails closed: blocks new scanner-driven entries, preserves
    existing-position management
  - SPY alone unavailable does NOT produce UNKNOWN if other families healthy
  - Sector diversity cap (max 3 symbols per sector)
  - Tiered AUC selection (Tier-1 >= 0.55, Tier-2 > 0.50 best-of)
  - Fallback to core watchlist if nothing beats random chance
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import numpy as np
import pandas as pd

from app.ai.features import FEATURE_COLUMNS, build_training_set
from app.core.applog import applog
from app.core.gap_scanner import PreMarketGapScanner
from app.core.scanner_discovery_input import resolve_stage1_symbols
from app.core.scanner_selection import select_sector_diverse_momentum
from app.core.universe_discovery import DiscoveryCoordinator
from app.data.manager import DataManager
from app.risk.market_regime import (
    MarketRiskAggregator,
    MarketRiskState,  # re-exported for convenience
)


class ScannerOutcome(Enum):
    SUCCESS_NONEMPTY = "SUCCESS_NONEMPTY"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Backward-compatible tri-state alias (satisfies #87 / 046 regressions)
# ---------------------------------------------------------------------------

class SpyVolatilityState(Enum):
    """Backward-compatible alias; new code should use MarketRiskState directly.

    Kept so existing tests that mock ``check_spy_volatility`` continue to work.
    """
    SAFE    = "SAFE"
    PANIC   = "PANIC"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Market universe — ~200 symbols across 7 sectors
# ---------------------------------------------------------------------------
MARKET_UNIVERSE: dict[str, list[str]] = {
    "mega_cap": [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL", "GOOG",
        "UNH", "XOM", "JPM", "LLY", "V", "MA", "AVGO", "JNJ", "PG",
        "COST", "HD", "MRK", "ABBV", "CVX", "WMT", "KO", "PEP",
    ],
    "large_cap_tech": [
        "AMD", "MU", "QCOM", "INTC", "TXN", "AMAT", "LRCX", "KLAC",
        "ADI", "MCHP", "MRVL", "NXPI", "CRM", "ADBE", "NOW", "SNOW",
        "PANW", "ZS", "CRWD", "NET", "DDOG", "MDB", "U", "PLTR", "CBRS",
    ],
    "financials": [
        "BAC", "GS", "MS", "WFC", "AXP", "BLK", "SCHW", "C",
        "COF", "USB", "PNC", "TFC", "SPGI", "ICE", "CME", "CBOE",
        "PYPL", "SQ", "COIN", "HOOD",
    ],
    "health": [
        "TMO", "ISRG", "BSX", "MDT", "SYK", "EW", "ABT", "GILD",
        "REGN", "VRTX", "BMY", "AMGN", "MRNA", "BIIB", "HUM", "CI",
        "CVS", "MCK", "ZBH", "BAX",
    ],
    "consumer_energy": [
        "MCD", "NKE", "SBUX", "TGT", "LOW", "TJX", "LULU", "ROST",
        "DG", "DLTR", "YUM", "CMG", "DPZ", "MNST", "DIS", "NFLX",
        "OXY", "SLB", "HAL", "DVN",
    ],
    "high_beta": [
        "RIVN", "SOFI", "MARA", "LCID", "DKNG", "UPST", "AI", "HOOD",
        "UBER", "LYFT", "ABNB", "DASH", "SNAP", "PINS", "RBLX", "TWLO",
        "ROKU", "SHOP", "SPOT", "HIMS",
    ],
    "sector_etfs": [
        "SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU",
        "XLB", "XLRE", "GLD", "SLV", "USO", "TLT", "HYG",
    ],
}

# Backward-compatible flat pool (used by backtest engine loader)
CANDIDATE_POOL: list[str] = sorted({
    sym for syms in MARKET_UNIVERSE.values() for sym in syms
})

# Reverse map: symbol -> sector
SYMBOL_SECTOR: dict[str, str] = {
    sym: sector
    for sector, syms in MARKET_UNIVERSE.items()
    for sym in syms
}

DEFENSIVE_WATCHLIST: list[str] = ["SPY", "QQQ", "DIA", "IWM", "GLD"]
LARGE_CAP_ANCHORS: list[str] = ["NVDA", "TSLA", "AMD", "META", "AMZN", "AAPL", "MSFT", "PLTR", "SMCI", "COIN"]

# Concurrency limits
_PREFILTER_CONCURRENCY = 5
_ML_CONCURRENCY = 4

# =============================================================================
# [MODIFIED 2026-10-22]: Expand scanner selection limits to hard cap 40
# ORIGINAL INTENT: _TECH_SCORE_TOP_N = 35, _MAX_PER_SECTOR = 5, and _TARGET_SYMBOLS = 24
# limited the scanner output to at most 24 symbols (and at most 35 across 7 sectors).
# REASON FOR CHANGE: User directive to expand scanner hard cap to 40 symbols. Expanded
# Stage 2 technical score top N to 60 and max per sector to 7 so 40 symbols can pass
# through ML validation without being bottlenecked at Stage 2 or sector caps.
# SAFE ROLLBACK: The original code is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
# # Selection parameters
# _TECH_SCORE_TOP_N = 35
# _MAX_PER_SECTOR = 5
# _TARGET_SYMBOLS = 24
# -----------------------------------------------------------------------------
# [NEW CODE]:
# Selection parameters
_TECH_SCORE_TOP_N = 60
_MAX_PER_SECTOR = 7
_TARGET_SYMBOLS = 40
# ==============================================================================
_AUC_TIER1 = 0.55
_AUC_TIER2 = 0.50


class MarketScanner:
    def __init__(self, data_manager: DataManager, core_watchlist: list[str], risk_config=None) -> None:
        self.data = data_manager
        self.core_watchlist = core_watchlist
        self.risk_config = risk_config
        self._discovery: DiscoveryCoordinator | None = None
        # F-11 FIX: was a class-level mutable list, shared across all instances.
        # Parallel backtests or tests with multiple scanner instances would silently
        # overwrite each other's scan state. Instance attribute provides isolation.
        self._last_scans: list[dict] = []
        self._last_discovery_telemetry: dict | None = None
        self._regime = MarketRiskAggregator(
            data_manager=data_manager,
            universe_symbols=list(CANDIDATE_POOL),
        )
        from app.config import get_settings
        cfg = self.risk_config or get_settings().risk_config()
        self._gap_scanner = PreMarketGapScanner(data_manager=data_manager, min_price=cfg.min_price)

    async def _stage1_symbols(self, as_of: datetime | None = None) -> list[str]:
        """Resolve bounded Stage-1 input without current-data replay leakage."""
        self._last_discovery_telemetry = None
        provider = self.data.provider
        discovery_supported = bool(getattr(provider, "supports_asset_discovery", False))
        acquire_discovery = None
        if as_of is None and discovery_supported:
            discovery = getattr(self, "_discovery", None)
            if discovery is None:
                discovery = getattr(self.data, "_discovery", None)
            if discovery is None:
                discovery = DiscoveryCoordinator(
# =============================================================================
# [MODIFIED 2026-10-22]: Expand DiscoveryCoordinator shortlist_limit from 50 to 80
# ORIGINAL INTENT: shortlist_limit=50 was sized for the prior 24-symbol cap.
# REASON FOR CHANGE: Sized to 80 to feed sufficient candidates for the 40 hard cap.
# SAFE ROLLBACK: The original code is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
#                     provider, feed="iex", asset_cache_ttl=timedelta(hours=6),
#                     batch_size=200, request_budget=10, shortlist_limit=50,
#                     result_cache_ttl=timedelta(minutes=15),
# -----------------------------------------------------------------------------
# [NEW CODE]:
                    provider, feed="iex", asset_cache_ttl=timedelta(hours=6),
                    batch_size=200, request_budget=10, shortlist_limit=80,
                    result_cache_ttl=timedelta(minutes=15),
# =============================================================================
                )
                self._discovery = discovery
                self.data._discovery = discovery
            async def _acquire_discovery():
                result = await discovery.acquire()
                telemetry = getattr(result, "telemetry", None)
                if is_dataclass(telemetry):
                    self._last_discovery_telemetry = asdict(telemetry)
                elif isinstance(telemetry, dict):
                    self._last_discovery_telemetry = dict(telemetry)
                return result

            acquire_discovery = _acquire_discovery
# =============================================================================
# [MODIFIED 2026-10-22]: Expand resolve_stage1_symbols fallback_limit from 50 to 80
# ORIGINAL INTENT: fallback_limit=50 bounded Stage 1 candidate pool for the 24 cap.
# REASON FOR CHANGE: Sized to 80 to ensure at least 40 candidates survive pre-filter and ML validation.
# SAFE ROLLBACK: The original code is preserved below as comments.
# -----------------------------------------------------------------------------
# [PREVIOUS CODE - PRESERVED]:
#         symbols = await resolve_stage1_symbols(
#             as_of=as_of, static_symbols=CANDIDATE_POOL, core_symbols=self.core_watchlist, fallback_limit=50,
#             discovery_supported=discovery_supported, acquire_discovery=acquire_discovery,
#         )
# -----------------------------------------------------------------------------
# [NEW CODE]:
        symbols = await resolve_stage1_symbols(
            as_of=as_of, static_symbols=CANDIDATE_POOL, core_symbols=self.core_watchlist, fallback_limit=80,
            discovery_supported=discovery_supported, acquire_discovery=acquire_discovery,
        )
# =============================================================================
        symbols_list = list(symbols)

        # Replay inputs intentionally carry SPY/QQQ/VIX even when they are only
        # market-risk context.  Keep the historical scanner causal and avoid
        # sending missing-bar symbols through every Stage-1 filter: only scan
        # candidate symbols that actually exist in the immutable replay input.
        available_symbols = getattr(self.data, "available_symbols", None)
        if as_of is not None and available_symbols is not None:
            available = {str(symbol).upper() for symbol in available_symbols}
            symbols_list = [
                symbol
                for symbol in symbols_list
                if symbol in available and symbol not in {"SPY", "QQQ", "VIX", "^VIX"}
            ]

        # Inject gap movers and intraday top gainers/losers only in live/production mode
        if as_of is None:
            try:
                dynamic_movers = await self._gap_scanner.get_gap_movers()
                if dynamic_movers:
                    applog.info("scanner", f"[Discovery] Injected {len(dynamic_movers)} dynamic movers into Stage 1 pool.")
                    symbols_list.extend(dynamic_movers)
            except Exception as exc:
                applog.error("scanner", f"[Discovery] Failed to fetch dynamic movers: {exc}")

        return sorted(list(set(symbols_list)))

    # ------------------------------------------------------------------
    # Global safety gate — multi-signal, fail-closed on UNKNOWN
    # ------------------------------------------------------------------

    async def check_spy_volatility(self, as_of: datetime | None = None) -> SpyVolatilityState:
        """Backward-compatible SPY gate wrapper.

        Delegates to MarketRiskAggregator.  STRESS/PANIC → PANIC;
        UNKNOWN → UNKNOWN; NORMAL/CAUTION → SAFE.
        Kept for tests that mock this method directly.
        """
        state, _conf, _cov, _reasons, _healths = await self._regime.assess(as_of=as_of)
        if state in (MarketRiskState.PANIC, MarketRiskState.STRESS):
            return SpyVolatilityState.PANIC
        if state is MarketRiskState.UNKNOWN:
            return SpyVolatilityState.UNKNOWN
        return SpyVolatilityState.SAFE

    async def _assess_market_risk(
        self, as_of: datetime | None = None
    ) -> tuple[MarketRiskState, float, list[str]]:
        """Run the full regime aggregator and return (state, confidence, reason_codes)."""
        state, conf, cov, reasons, healths = await self._regime.assess(as_of=as_of)
        applog.info(
            "scanner",
            f"[MarketRisk] state={state.value} confidence={conf:.2f} coverage={cov:.2f} "
            f"reasons={reasons} "
            f"unavailable={[f for f, h in healths.items() if not h.healthy]}",
        )
        return state, conf, reasons

    # ------------------------------------------------------------------
    # Stage 1 — Parallel pre-filter
    # ------------------------------------------------------------------

    async def _stage1_prefilter_parallel(
        self, as_of: datetime | None = None, symbols: list[str] | None = None
    ) -> list[dict]:
        """Fetch daily stats for all ~200 symbols concurrently. Returns candidates."""
        end = as_of or datetime.now(UTC)
        start = end - timedelta(days=25)
        all_symbols = list(symbols) if symbols is not None else CANDIDATE_POOL
        semaphore = asyncio.Semaphore(_PREFILTER_CONCURRENCY)

        applog.info("scanner", f"[Stage 1] Pre-filtering {len(all_symbols)} symbols (parallel)... end={end.isoformat()} tzinfo={end.tzinfo}")

        diag_records = {}
        history_fn = self.data.history

        multi_1d: dict[str, pd.DataFrame] = {}
        multi_1m: dict[str, pd.DataFrame] = {}
        if hasattr(self.data, "multi_history"):
            try:
                res_1d = await self.data.multi_history(all_symbols, "1Day", start, end, prefer_cache=True)
                if isinstance(res_1d, dict):
                    multi_1d = res_1d
                res_1m = await self.data.multi_history(all_symbols, "1Min", end - timedelta(minutes=15), end, prefer_cache=True)
                if isinstance(res_1m, dict):
                    multi_1m = res_1m
            except Exception as exc:
                applog.error("scanner", f"Failed multi_history batch fetch: {exc}")

        async def _check(symbol: str) -> dict | None:
            symbol = symbol.upper()
            record = {
                "symbol": symbol,
                "T": end.isoformat(),
                "adapter": self.data.provider.__class__.__name__,
                "timeframe": "1Day",
                "required_bars": 8,
                "bars_raw": 0,
                "bars_asof": 0,
                "first_bar": "N/A",
                "latest_bar": "N/A",
                "price": "N/A",
                "avg_dollar_volume": "N/A",
                "filter_liquidity": {"actual": 0.0, "threshold": 0.0, "result": "FAIL"},
                "filter_price": {"actual": 0.0, "min": 0.0, "result": "FAIL"},
                "filter_history": {"actual": 0, "required": 8, "result": "FAIL"},
                "filter_staleness": {"actual": "N/A", "max_lag": "N/A", "result": "FAIL"},
                "FINAL_PASS": False,
                "FINAL_REJECTION_REASON": "MISSING_DATA"
            }
            diag_records[symbol] = record

            async with semaphore:
                try:
                    if symbol in multi_1d:
                        df = multi_1d[symbol]
                    else:
                        df = await history_fn(symbol, "1Day", start, end, prefer_cache=True)

                    record["bars_raw"] = len(df) if df is not None else 0
                    record["bars_asof"] = len(df) if df is not None else 0

                    record["filter_history"] = {
                        "actual": len(df),
                        "required": 8,
                        "result": "PASS" if len(df) >= 8 else "FAIL"
                    }
                    if df.empty:
                        record["FINAL_REJECTION_REASON"] = "MISSING_DATA"
                        return None

                    record["first_bar"] = df.index[0].isoformat()
                    record["latest_bar"] = df.index[-1].isoformat()

                    if len(df) < 8:
                        record["FINAL_REJECTION_REASON"] = "INSUFFICIENT_HISTORY"
                        return None

                    latest_bar_ts = df.index[-1]
                    from zoneinfo import ZoneInfo
                    end_pd = pd.Timestamp(end)
                    now_tz = end_pd.tz_convert(latest_bar_ts.tz) if latest_bar_ts.tz else end_pd
                    now_et = now_tz.astimezone(ZoneInfo("America/New_York"))
                    et_mins = now_et.hour * 60 + now_et.minute

                    # Allow prior completed session daily bars (up to 96 hours / 4 calendar days
                    # to accommodate long weekends and market holidays). A prior-session completed
                    # 1Day bar is expected to be 17-24+ hours old during regular market hours.
                    allowed_lag = timedelta(hours=96)

                    raw_lag = now_tz - latest_bar_ts
                    time_since_midnight = now_et - now_et.replace(hour=0, minute=0, second=0, microsecond=0)
                    lag = max(timedelta(0), raw_lag - time_since_midnight)

                    record["filter_staleness"] = {
                        "actual": str(lag),
                        "max_lag": str(allowed_lag),
                        "result": "PASS" if lag <= allowed_lag else "FAIL"
                    }
                    if lag > allowed_lag:
                        record["FINAL_REJECTION_REASON"] = "STALE_DATA"
                        return None

                    price = float(df["close"].iloc[-1])
                    intraday_df = None
                    try:
                        if symbol in multi_1m:
                            intraday_df = multi_1m[symbol]
                        else:
                            intraday_df = await history_fn(symbol, "1Min", end - timedelta(minutes=15), end, prefer_cache=True)
                        if intraday_df is not None and not intraday_df.empty:
                            price = float(intraday_df["close"].iloc[-1])
                    except Exception:
                        pass
                    record["price"] = price

                    from app.config import get_settings
                    cfg = self.risk_config or get_settings().risk_config()

                    record["filter_price"] = {
                        "actual": price,
                        "min": cfg.min_price,
                        "result": "PASS" if price >= cfg.min_price else "FAIL"
                    }
                    if price < cfg.min_price:
                        record["FINAL_REJECTION_REASON"] = "PRICE_FILTER"
                        return None

                    avg_vol = float(df["volume"].mean())
                    avg_daily_dollar_vol = avg_vol * price
                    target_daily_dollar_vol = cfg.min_avg_dollar_volume * 390

                    # Time-normalized RVOL calculation for partial session comparison
                    if et_mins >= 9 * 60 + 30 and et_mins <= 16 * 60:
                        session_elapsed_frac = min(1.0, max(0.05, (et_mins - (9 * 60 + 30)) / 390.0))
                    else:
                        session_elapsed_frac = 1.0

                    last_bar_vol = float(df["volume"].iloc[-1])
                    normalized_last_vol = last_bar_vol / session_elapsed_frac if session_elapsed_frac > 0 else last_bar_vol
                    rvol = float(normalized_last_vol / avg_vol) if avg_vol > 0 else 0.0

                    # Use Projected Catalyst Liquidity (allow huge intraday runners to pass filter)
                    projected_daily_dollar_vol = avg_daily_dollar_vol * max(1.0, rvol)

                    record["avg_dollar_volume"] = avg_daily_dollar_vol
                    record["filter_liquidity"] = {
                        "actual": projected_daily_dollar_vol,
                        "threshold": target_daily_dollar_vol,
                        "result": "PASS" if projected_daily_dollar_vol >= target_daily_dollar_vol else "FAIL"
                    }
                    if projected_daily_dollar_vol < target_daily_dollar_vol:
                        record["FINAL_REJECTION_REASON"] = "LIQUIDITY_FILTER"
                        return None

                    record["FINAL_PASS"] = True
                    record["FINAL_REJECTION_REASON"] = "NONE"

                    atr_pct = float(((df["high"] - df["low"]) / df["close"] * 100.0).mean())
                    yesterday_close = float(df["close"].iloc[-2]) if len(df) >= 2 else price
                    today_open = float(df["open"].iloc[-1])
                    gap_pct = abs(today_open / yesterday_close - 1.0) * 100.0 if yesterday_close > 0 else 0.0
                    mom3 = float((price / df["close"].iloc[-4] - 1.0) * 100.0) if len(df) >= 4 else 0.0
                    return {
                        "symbol": symbol,
                        "sector": SYMBOL_SECTOR.get(symbol, "unknown"),
                        "price": price,
                        "avg_vol": avg_vol,
                        "rvol": rvol,
                        "atr_pct": atr_pct,
                        "gap_pct": gap_pct,
                        "mom3": mom3,
                        "latest_bar_timestamp": latest_bar_ts,
                    }
                except Exception as exc:
                    applog.error("scanner", f"Exception during Stage 1 check for {symbol}: {exc.__class__.__name__}: {exc}")
                    record["FINAL_REJECTION_REASON"] = "CALCULATION_ERROR"
                    return None

        results = await asyncio.gather(*[_check(sym) for sym in all_symbols])
        candidates = [r for r in results if r is not None]

        # Aggregate rejection reasons for diagnostics
        rejection_counts = {}
        for rec in diag_records.values():
            if not rec.get("FINAL_PASS"):
                reason = rec.get("FINAL_REJECTION_REASON", "UNKNOWN")
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

        self._last_stage1_rejections = rejection_counts
        applog.info(
            "scanner",
            f"[Stage 1] {len(candidates)}/{len(all_symbols)} symbols passed pre-filter. Rejections: {rejection_counts}",
        )
        return candidates

    # ------------------------------------------------------------------
    # Stage 2 — Technical composite score (no extra API calls)
    # ------------------------------------------------------------------

    @staticmethod
    def _stage2_technical_score(candidates: list[dict], as_of: datetime | None = None) -> list[dict]:
        if not candidates:
            return []

        from datetime import UTC
        T = as_of or datetime.now(UTC)

        rvols = np.array([c["rvol"] for c in candidates])
        atrs = np.array([c["atr_pct"] for c in candidates])
        gaps = np.array([c["gap_pct"] for c in candidates])
        # Audit Fix: Use absolute magnitude so massive shorts aren't pushed to rank 0.0
        moms = np.array([abs(c["mom3"]) for c in candidates])

        def _rank(arr: np.ndarray) -> np.ndarray:
            order = arr.argsort()
            ranks = np.empty_like(order, dtype=float)
            ranks[order] = np.arange(len(arr)) / max(len(arr) - 1, 1)
            return ranks

        rvol_rank = _rank(rvols)
        atr_rank = _rank(atrs)
        gap_rank = _rank(gaps)
        mom_rank = _rank(moms)

        scores = 0.30 * rvol_rank + 0.20 * gap_rank + 0.20 * atr_rank + 0.30 * mom_rank
        for i, c in enumerate(candidates):
            c["tech_score"] = float(scores[i])
            applog.info(
                "scanner",
                f"SCAN_SCORE: symbol={c['symbol']}, T={T.isoformat()}, "
                f"rel_volume={c['rvol']:.4f}, ATR_pct={c['atr_pct']:.4f}, "
                f"momentum={c['mom3']:.4f}, VWAP_distance=N/A, "
                f"composite_score={c['tech_score']:.4f}, stage=TECH_SCORE"
            )

        ranked = sorted(candidates, key=lambda c: c["tech_score"], reverse=True)
        return ranked[:_TECH_SCORE_TOP_N]

    # ------------------------------------------------------------------
    # Stage 3 — ML AUC validation
    # ------------------------------------------------------------------

    async def _stage3_ml_validate(
        self, candidates: list[dict], as_of: datetime | None = None
    ) -> list[dict]:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit

        end = as_of or datetime.now(UTC)
        start = end - timedelta(days=20)
        semaphore = asyncio.Semaphore(_ML_CONCURRENCY)

        async def _validate(candidate: dict) -> dict:
            async with semaphore:
                symbol = candidate["symbol"]
                try:
                    # =============================================================================
                    # [MODIFIED 2026-10-26]: Deduplicate History Index in Scanner ML Validation
                    # ORIGINAL INTENT: Fetched bar history from self.data.history and checked length directly.
                    # REASON FOR CHANGE: If live data feeds or multi-provider caches return duplicate timestamp labels,
                    # downstream build_training_set and feature indexing crash with 'cannot reindex on an axis with duplicate labels'.
                    # Deduplicating the DatetimeIndex guarantees clean feature extraction and ML model training.
                    # SAFE ROLLBACK: The original code is preserved below as comments.
                    # -----------------------------------------------------------------------------
                    # [PREVIOUS CODE - PRESERVED]:
                    #                     df = await self.data.history(symbol, "1Min", start, end, prefer_cache=True)
                    #                     if len(df) < 500:
                    #                         candidate["auc"] = 0.0
                    #                         return candidate
                    # -----------------------------------------------------------------------------
                    # [NEW CODE]:
                    df = await self.data.history(symbol, "1Min", start, end, prefer_cache=True)
                    if df.index.has_duplicates:
                        df = df[~df.index.duplicated(keep="last")]
                    if len(df) < 500:
                        candidate["auc"] = 0.0
                        return candidate
                    # =============================================================================

                    X, y, _ = build_training_set(df)
                    if len(X) < 100:
                        candidate["auc"] = 0.0
                        return candidate

                    if y.nunique() < 2:
                        # Audit Fix: Target class imbalance implies massive one-sided trend.
                        # Do not kill it with AUC 0.0, give it a passing baseline so the main NN can evaluate.
                        candidate["auc"] = 0.55
                        return candidate

                    def _train_and_evaluate(X_sync, y_sync):
                        tscv_sync = TimeSeriesSplit(n_splits=3)
                        aucs_sync = []
                        for train_idx, val_idx in tscv_sync.split(X_sync):
                            y_train = y_sync.iloc[train_idx]
                            if y_train.nunique() < 2:
                                continue
                            model = RandomForestClassifier(
                                n_estimators=50, max_depth=5, min_samples_leaf=10,
                                random_state=42, n_jobs=1,
                            )
                            model.fit(X_sync.iloc[train_idx][FEATURE_COLUMNS], y_train)
                            probs = model.predict_proba(X_sync.iloc[val_idx][FEATURE_COLUMNS])[:, 1]
                            try:
                                aucs_sync.append(roc_auc_score(y_sync.iloc[val_idx], probs))
                            except ValueError:
                                continue
                        return float(np.mean(aucs_sync)) if aucs_sync else 0.0

                    candidate["auc"] = await asyncio.to_thread(_train_and_evaluate, X, y)
                except Exception as exc:
                    applog.error("scanner", f"ML validation failed for {symbol}: {exc}")
                    candidate["auc"] = 0.0
                return candidate

        results = await asyncio.gather(*[_validate(c) for c in candidates])
        return list(results)

    # ------------------------------------------------------------------
    # Final selection — tiered AUC + sector diversity
    # ------------------------------------------------------------------

    @staticmethod
    def _select_final(candidates: list[dict]) -> list[str]:
        if not candidates:
            return []

        ranked = sorted(candidates, key=lambda c: c.get("auc", 0.0), reverse=True)
        tier1 = [c for c in ranked if c.get("auc", 0.0) >= _AUC_TIER1]
        tier2 = [c for c in ranked if _AUC_TIER2 < c.get("auc", 0.0) < _AUC_TIER1]
        # The shared helper is the production selection boundary. Keeping both
        # tiers in one ordered input preserves the tier-1 preference while
        # enforcing one sector-cap implementation for discovered and static
        # symbols alike.
        return select_sector_diverse_momentum(
            [*tier1, *tier2],
            max_symbols=_TARGET_SYMBOLS,
            max_per_sector=_MAX_PER_SECTOR,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def screen_pool(self, as_of: datetime | None = None) -> list[dict]:
        symbols = await self._stage1_symbols(as_of=as_of)
        return await self._stage1_prefilter_parallel(as_of=as_of, symbols=symbols)

    async def run(self, as_of: datetime | None = None) -> tuple[ScannerOutcome, list[str]]:
        """Execute the full 3-stage scan. Returns (outcome, watchlist).

        MarketRiskState gate behaviour:
          NORMAL / CAUTION  -- proceed with normal candidate pipeline.
          STRESS / PANIC    -- return DEFENSIVE_WATCHLIST immediately.
          UNKNOWN           -- fail closed: SUCCESS_EMPTY so the engine preserves
                              existing-position management without authorising
                              new scanner-driven entries.
        """
        # A successful scan with no final candidates must not expose the prior
        # cycle's Stage-3 rows to engine/replay telemetry.
        self._last_scans = []
        try:
            market_state, confidence, reason_codes = await self._assess_market_risk(as_of=as_of)

            # =============================================================================
            # [MODIFIED 2026-10-22]: Preserve Candidate Scanning in STRESS and Configurable PANIC
            # ORIGINAL INTENT: STRESS and PANIC immediately aborted candidate scanning and
            # returned DEFENSIVE_WATCHLIST (["SPY", "QQQ", "DIA", "IWM", "GLD"]), purging all
            # 40 active mover equities and halting all trading.
            # REASON FOR CHANGE: STRESS is normal volatility/tech divergence and should never halt
            # candidate scanning. In PANIC, dynamic candidate discovery is preserved unless
            # panic_deploy_defensive_watchlist is explicitly enabled, allowing short breakdown
            # trades and high-volume movers to be captured while defensive anchors are appended.
            # SAFE ROLLBACK: Previous lines preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # # STRESS/PANIC are capital-preservation regimes. We swap to a small
            # # defensive universe instead of continuing to chase the full pool.
            # if market_state in (MarketRiskState.PANIC, MarketRiskState.STRESS):
            #     applog.warning(
            #         "scanner",
            #         f"Market regime={market_state.value} (confidence={confidence:.2f}). "
            #         f"Deploying defensive watchlist. reasons={reason_codes}",
            #     )
            #     return ScannerOutcome.SUCCESS_NONEMPTY, DEFENSIVE_WATCHLIST.copy()
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            deploy_defensive_only = (
                getattr(self.risk_config, "panic_deploy_defensive_watchlist", True if self.risk_config is None else False)
            )
            if market_state in (MarketRiskState.PANIC, MarketRiskState.STRESS) and deploy_defensive_only:
                applog.warning(
                    "scanner",
                    f"Market regime={market_state.value} (confidence={confidence:.2f}). "
                    f"Deploying defensive watchlist (legacy fallback). reasons={reason_codes}",
                )
                return ScannerOutcome.SUCCESS_NONEMPTY, DEFENSIVE_WATCHLIST.copy()

            if market_state in (MarketRiskState.PANIC, MarketRiskState.STRESS):
                applog.warning(
                    "scanner",
                    f"Market regime={market_state.value} (confidence={confidence:.2f}). "
                    f"Proceeding with candidate pipeline; directional risk managed downstream. reasons={reason_codes}",
                )
            # =============================================================================

            # UNKNOWN means the regime view is not trustworthy enough to
            # authorize new scanner-driven risk.
            if market_state is MarketRiskState.UNKNOWN:
                applog.warning(
                    "scanner",
                    f"Market regime=UNKNOWN (confidence={confidence:.2f}). "
                    "Failing closed: blocking new scanner-driven entries. "
                    "Existing-position management continues unaffected. "
                    f"reasons={reason_codes}",
                )
                return ScannerOutcome.SUCCESS_EMPTY, []

            # NORMAL or CAUTION keep the normal scan path alive. CAUTION is
            # handled upstream by tighter thresholds and smaller sizing.
            if market_state is MarketRiskState.CAUTION:
                applog.warning(
                    "scanner",
                    f"Market regime=CAUTION (confidence={confidence:.2f}). "
                    "Proceeding with pipeline at reduced confidence. "
                    f"reasons={reason_codes}",
                )

            stage1_symbols = await self._stage1_symbols(as_of=as_of)
            stage1 = await self._stage1_prefilter_parallel(as_of=as_of, symbols=stage1_symbols)
            if not stage1:
                rejections = getattr(self, "_last_stage1_rejections", {})
                data_errors = rejections.get("STALE_DATA", 0) + rejections.get("MISSING_DATA", 0) + rejections.get("CALCULATION_ERROR", 0)
                if data_errors > 0 and data_errors >= 0.5 * len(stage1_symbols):
                    applog.warning("scanner", f"Stage 1 FAILED due to systemic data infrastructure issues. Rejections: {rejections}")
                    return ScannerOutcome.FAILED, []
                applog.warning("scanner", f"Stage 1 returned no candidates due to market filters. Rejections: {rejections}")
                return ScannerOutcome.SUCCESS_EMPTY, []

            stage2 = self._stage2_technical_score(stage1, as_of=as_of)
            stage3 = await self._stage3_ml_validate(stage2, as_of=as_of)
            final = self._select_final(stage3)
            # =============================================================================
            # [MODIFIED 2026-10-22]: Append defensive anchors in PANIC alongside discovered candidates
            # ORIGINAL INTENT: Candidates were selected with no defensive anchors in normal flow.
            # REASON FOR CHANGE: In PANIC, keep both the active alpha mover candidates AND macro anchors
            # (SPY, QQQ) so the engine can monitor index health while trading candidate breakdowns.
            # SAFE ROLLBACK: Previous lines preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # if not final:
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            if market_state is MarketRiskState.PANIC and final:
                defensive_anchors = [s for s in DEFENSIVE_WATCHLIST if s not in final]
                final = final + defensive_anchors[:5]

            if not final:
            # =============================================================================
                applog.warning("scanner", "No candidates beat AUC > 0.50.")
                return ScannerOutcome.SUCCESS_EMPTY, []

            applog.info(
                "scanner",
                f"Scan complete: {len(final)} symbols selected from {len(CANDIDATE_POOL)} candidates. "
                f"Selected: {final}",
            )
            self._last_scans = [
                {
                    "symbol": c["symbol"],
                    "score": round(c.get("tech_score", 0.0), 4),
                    "auc": round(c.get("auc", 0.0), 4),
                    "sector": c.get("sector", "unknown"),
                    "rvol": round(c.get("rvol", 0.0), 2),
                    "atr_pct": round(c.get("atr_pct", 0.0), 2),
                }
                for c in stage3
            ]
            return ScannerOutcome.SUCCESS_NONEMPTY, final
        except Exception as exc:
            applog.error("scanner", f"Scanner pipeline failed: {exc}")
            return ScannerOutcome.FAILED, []

    @property
    def mover_context(self) -> dict[str, dict]:
        """Live gainer/loser context from the gap/movers scanner.

        {symbol: {"direction": "gainer"|"loser", "change_pct": float}}
        Consumed by strategy evaluation for directional bias.
        """
        return getattr(self._gap_scanner, "mover_context", {}) or {}

    def get_last_scans(self) -> list[dict]:
        return list(self._last_scans)
