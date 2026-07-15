"""MarketScanner -- parallel swarm scanner covering ~200 symbols across 7 market sectors.

Pipeline:
  Stage 1  Fast pre-filter  (asyncio.gather, semaphore=20)  ~200 symbols -> ~60
  Stage 2  Technical score  (instant, no extra API calls)   ~60 -> top 25
  Stage 3  ML AUC validate  (asyncio.gather, semaphore=8)   top 25 -> final 10-15

Safety gates:
  - Global SPY panic halt (>= 85th volatility percentile)
  - Sector diversity cap (max 3 symbols per sector)
  - Tiered AUC selection (Tier-1 >= 0.55, Tier-2 > 0.50 best-of)
  - Fallback to core watchlist if nothing beats random chance
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from enum import Enum

import numpy as np
import pandas as pd

from app.core.logging import applog
from app.core.scanner_discovery_input import resolve_stage1_symbols
from app.core.universe_discovery import DiscoveryCoordinator
from app.ai.features import FEATURE_COLUMNS, build_training_set
from app.data.manager import DataManager

class ScannerOutcome(Enum):
    SUCCESS_NONEMPTY = "SUCCESS_NONEMPTY"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    FAILED = "FAILED"


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
_PREFILTER_CONCURRENCY = 20   # Stage 1: API calls (I/O-bound)
_ML_CONCURRENCY = 8           # Stage 3: sklearn training (CPU-bound)

# Selection parameters
_TECH_SCORE_TOP_N = 25        # Stage 2 → Stage 3 handoff
_MAX_PER_SECTOR = 3           # Sector diversity cap
_TARGET_SYMBOLS = 12          # Ideal final watchlist size
_AUC_TIER1 = 0.55             # Strong edge threshold
_AUC_TIER2 = 0.50             # Beats random (minimum acceptable)


class MarketScanner:
    _last_scans = []

    def __init__(self, data_manager: DataManager, core_watchlist: list[str]) -> None:
        self.data = data_manager
        self.core_watchlist = core_watchlist
        self._discovery: DiscoveryCoordinator | None = None

    async def _stage1_symbols(self, as_of: datetime | None = None) -> list[str]:
        """Resolve bounded Stage-1 input without current-data replay leakage."""
        provider = self.data.provider
        discovery_supported = bool(getattr(provider, "supports_asset_discovery", False))
        acquire_discovery = None
        if as_of is None and discovery_supported:
            discovery = getattr(self.data, "_discovery", None)
            if discovery is None:
                discovery = DiscoveryCoordinator(
                    provider, feed="iex", asset_cache_ttl=timedelta(hours=6),
                    batch_size=200, request_budget=10, shortlist_limit=50,
                )
                self.data._discovery = discovery
            acquire_discovery = discovery.acquire
        symbols = await resolve_stage1_symbols(
            as_of=as_of, static_symbols=CANDIDATE_POOL, fallback_limit=50,
            discovery_supported=discovery_supported, acquire_discovery=acquire_discovery,
        )
        return list(symbols)

    # ------------------------------------------------------------------
    # Global safety gate
    # ------------------------------------------------------------------

    async def check_spy_volatility(self, as_of: datetime | None = None) -> bool:
        """True = safe to trade. False = panic state, deploy defensives."""
        try:
            end = as_of or datetime.now(UTC)
            start = end - timedelta(days=20)
            df = await self.data.history("SPY", "1Min", start, end, prefer_cache=True)
            if df.empty or len(df) < 200:
                return True

            df_h = df.resample("1h").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna()
            if len(df_h) < 20:
                return True

            prev = df_h["close"].shift(1)
            tr = pd.concat([
                df_h["high"] - df_h["low"],
                (df_h["high"] - prev).abs(),
                (df_h["low"] - prev).abs(),
            ], axis=1).max(axis=1)
            atr_pct = tr.ewm(alpha=1.0 / 14, adjust=False).mean() / df_h["close"] * 100.0

            if atr_pct.std() < 1e-6:
                vol_pct = 0.0
            else:
                vol_pct = float((atr_pct.tail(200) <= atr_pct.iloc[-1]).mean())

            if vol_pct >= 0.85:
                applog.warning(
                    "scanner",
                    f"Market panic: SPY volatility at {vol_pct*100:.1f}th percentile. "
                    "Deploying defensive ETFs.",
                )
                return False

            applog.info("scanner", f"Market volatility normal (SPY Volatility Percentile: {vol_pct*100:.1f}%)")
            return True
        except Exception as exc:
            applog.warning("scanner", f"SPY volatility check failed: {exc}. Assuming safe.")
            return True

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

        history_fn = getattr(self.data, "original_history", self.data.history)
        
        async def _check(symbol: str) -> dict | None:
            symbol = symbol.upper()
            record = {
                "symbol": symbol,
                "T": end.isoformat(),
                "adapter": "HistoricalAsOfDataAdapter" if hasattr(self.data, "original_history") else self.data.provider.__class__.__name__,
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
                    df = await history_fn(symbol, "1Day", start, end, prefer_cache=True)
                    # Diagnostics describe the authoritative Stage-1 dataset. They
                    # must never issue a second provider request just to count raw bars.
                    record["bars_raw"] = len(df)
                    record["bars_asof"] = len(df)

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
                    
                    # Allow up to 100 hours before 9:30 AM to account for weekends and holidays
                    allowed_lag = timedelta(hours=100) if et_mins < 9 * 60 + 30 else timedelta(hours=1)
                    
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
                    try:
                        intraday_df = await history_fn(symbol, "1Min", end - timedelta(minutes=15), end, prefer_cache=True)
                        if not intraday_df.empty:
                            price = float(intraday_df["close"].iloc[-1])
                    except Exception:
                        pass
                    record["price"] = price
                    
                    from app.config import get_settings
                    cfg = get_settings().risk_config()
                    
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
                    
                    record["avg_dollar_volume"] = avg_daily_dollar_vol
                    record["filter_liquidity"] = {
                        "actual": avg_daily_dollar_vol,
                        "threshold": target_daily_dollar_vol,
                        "result": "PASS" if avg_daily_dollar_vol >= target_daily_dollar_vol else "FAIL"
                    }
                    if avg_daily_dollar_vol < target_daily_dollar_vol:
                        record["FINAL_REJECTION_REASON"] = "LIQUIDITY_FILTER"
                        return None

                    record["FINAL_PASS"] = True
                    record["FINAL_REJECTION_REASON"] = "NONE"

                    rvol = float(df["volume"].iloc[-1] / avg_vol) if avg_vol > 0 else 0.0
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
        applog.info(
            "scanner",
            f"[Stage 1] {len(candidates)}/{len(all_symbols)} symbols passed pre-filter.",
        )

        return candidates

    # ------------------------------------------------------------------
    # Stage 2 — Technical composite score (no extra API calls)
    # ------------------------------------------------------------------

    @staticmethod
    def _stage2_technical_score(candidates: list[dict], as_of: datetime | None = None) -> list[dict]:
        """Rank candidates by composite technical score. Returns sorted list."""
        if not candidates:
            return []

        from datetime import UTC
        T = as_of or datetime.now(UTC)

        rvols = np.array([c["rvol"] for c in candidates])
        atrs = np.array([c["atr_pct"] for c in candidates])
        gaps = np.array([c["gap_pct"] for c in candidates])
        moms = np.array([c["mom3"] for c in candidates])

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
            # Log component scores
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
        """Train quick per-symbol models. Returns candidates with AUC > 0.50."""
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit

        end = as_of or datetime.now(UTC)
        start = end - timedelta(days=60)
        semaphore = asyncio.Semaphore(_ML_CONCURRENCY)

        async def _validate(candidate: dict) -> dict:
            async with semaphore:
                symbol = candidate["symbol"]
                try:
                    df = await self.data.history(symbol, "1Min", start, end, prefer_cache=True)
                    if len(df) < 500:
                        candidate["auc"] = 0.0
                        return candidate

                    X, y = build_training_set(df)
                    if len(X) < 100 or y.nunique() < 2:
                        candidate["auc"] = 0.0
                        return candidate

                    # Time-series cross-validation: 3 folds, expanding window
                    tscv = TimeSeriesSplit(n_splits=3)
                    aucs: list[float] = []
                    for train_idx, val_idx in tscv.split(X):
                        model = RandomForestClassifier(
                            n_estimators=50, max_depth=5, min_samples_leaf=10,
                            random_state=42, n_jobs=1,
                        )
                        model.fit(X.iloc[train_idx][FEATURE_COLUMNS], y.iloc[train_idx])
                        probs = model.predict_proba(X.iloc[val_idx][FEATURE_COLUMNS])[:, 1]
                        try:
                            aucs.append(roc_auc_score(y.iloc[val_idx], probs))
                        except ValueError:
                            continue

                    candidate["auc"] = float(np.mean(aucs)) if aucs else 0.0
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
        """Tiered selection: Tier-1 strong AUC, then Tier-2 best-of to fill target."""
        if not candidates:
            return []

        # Sort by AUC descending
        ranked = sorted(candidates, key=lambda c: c.get("auc", 0.0), reverse=True)
        selected: list[str] = []
        sector_counts: dict[str, int] = {}

        def _try_add(c: dict) -> bool:
            sym = c["symbol"]
            sector = c.get("sector")
            if not isinstance(sector, str) or not sector.strip():
                sector = f"unknown:{sym}"
            if sector_counts.get(sector, 0) >= _MAX_PER_SECTOR:
                return False
            selected.append(sym)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            return True

        # Tier 1: AUC >= 0.55
        for c in ranked:
            if c.get("auc", 0.0) >= _AUC_TIER1:
                _try_add(c)
            if len(selected) >= _TARGET_SYMBOLS:
                break

        # Tier 2: AUC > 0.50, fill remaining slots
        if len(selected) < _TARGET_SYMBOLS:
            for c in ranked:
                if c["symbol"] in selected:
                    continue
                if c.get("auc", 0.0) > _AUC_TIER2:
                    _try_add(c)
                if len(selected) >= _TARGET_SYMBOLS:
                    break

        return selected

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def screen_pool(self, as_of: datetime | None = None) -> list[dict]:
        """Run Stage 1 pre-filter only. Used by tests and diagnostics."""
        symbols = await self._stage1_symbols(as_of=as_of)
        return await self._stage1_prefilter_parallel(as_of=as_of, symbols=symbols)

    async def run(self, as_of: datetime | None = None) -> tuple[ScannerOutcome, list[str]]:
        """Execute the full 3-stage scan. Returns (outcome, watchlist)."""
        try:
            # Global panic gate first
            safe = await self.check_spy_volatility(as_of=as_of)
            if not safe:
                return ScannerOutcome.SUCCESS_NONEMPTY, DEFENSIVE_WATCHLIST.copy()

            stage1_symbols = await self._stage1_symbols(as_of=as_of)
            stage1 = await self._stage1_prefilter_parallel(as_of=as_of, symbols=stage1_symbols)
            if not stage1:
                applog.warning("scanner", "Stage 1 returned no candidates. Falling back to core watchlist.")
                return ScannerOutcome.SUCCESS_EMPTY, []

            stage2 = self._stage2_technical_score(stage1, as_of=as_of)
            stage3 = await self._stage3_ml_validate(stage2, as_of=as_of)
            final = self._select_final(stage3)

            if not final:
                applog.warning("scanner", "No candidates beat AUC > 0.50. Falling back to core watchlist.")
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

    @classmethod
    def get_last_scans(cls) -> list[dict]:
        return cls._last_scans
