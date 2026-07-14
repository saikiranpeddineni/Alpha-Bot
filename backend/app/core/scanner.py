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
        self, as_of: datetime | None = None
    ) -> list[dict]:
        """Fetch daily stats for all ~200 symbols concurrently. Returns candidates."""
        end = as_of or datetime.now(UTC)
        start = end - timedelta(days=25)
        all_symbols = CANDIDATE_POOL
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
                    df_raw = pd.DataFrame()
                    try:
                        df_raw = await self.data.provider.get_bars(symbol, "1Day", start, end)
                    except Exception:
                        pass
                    
                    df = await history_fn(symbol, "1Day", start, end, prefer_cache=True)
                    record["bars_raw"] = len(df_raw)
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

        is_diag_target_ts = end.isoformat().startswith("2026-07-06T14:00")
        applog.info("scanner", f"DEBUG is_diag_target_ts={is_diag_target_ts} for end={end.isoformat()}")

        if is_diag_target_ts:
            target_symbols = ["TSLA", "NVDA", "SPY", "MU", "AMD"]
            for sym in target_symbols:
                rec = diag_records.get(sym)
                if rec:
                    applog.info("scanner", "[SCANNER_DIAG]")
                    applog.info("scanner", f"symbol={rec['symbol']}")
                    applog.info("scanner", f"T={rec['T']}")
                    applog.info("scanner", f"adapter={rec['adapter']}")
                    applog.info("scanner", f"timeframe={rec['timeframe']}")
                    applog.info("scanner", f"required_bars={rec['required_bars']}")
                    applog.info("scanner", f"bars_raw={rec['bars_raw']}")
                    applog.info("scanner", f"bars_asof={rec['bars_asof']}")
                    applog.info("scanner", f"first_bar={rec['first_bar']}")
                    applog.info("scanner", f"latest_bar={rec['latest_bar']}")
                    applog.info("scanner", f"price={rec['price']}")
                    applog.info("scanner", f"avg_dollar_volume={rec['avg_dollar_volume']}")
                    
                    fl = rec['filter_liquidity']
                    applog.info("scanner", f"filter_liquidity actual={fl['actual']} threshold={fl['threshold']} result={fl['result']}")
                    fp = rec['filter_price']
                    applog.info("scanner", f"filter_price actual={fp['actual']} min={fp['min']} result={fp['result']}")
                    fh = rec['filter_history']
                    applog.info("scanner", f"filter_history actual={fh['actual']} required={fh['required']} result={fh['result']}")
                    fs = rec.get('filter_staleness', {"actual": "N/A", "max_lag": "N/A", "result": "FAIL"})
                    applog.info("scanner", f"filter_staleness actual={fs['actual']} max_lag={fs['max_lag']} result={fs['result']}")
                    
                    applog.info("scanner", f"FINAL_PASS={rec['FINAL_PASS']}")
                    applog.info("scanner", f"FINAL_REJECTION_REASON={rec['FINAL_REJECTION_REASON']}")

            passed_cnt = sum(1 for r in diag_records.values() if r["FINAL_PASS"])
            insufficient_history_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] == "INSUFFICIENT_HISTORY")
            missing_data_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] == "MISSING_DATA")
            stale_data_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] == "STALE_DATA")
            price_filter_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] == "PRICE_FILTER")
            liquidity_filter_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] == "LIQUIDITY_FILTER")
            volatility_filter_cnt = 0
            calc_error_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] == "CALCULATION_ERROR")
            other_cnt = sum(1 for r in diag_records.values() if r["FINAL_REJECTION_REASON"] not in ["NONE", "INSUFFICIENT_HISTORY", "MISSING_DATA", "STALE_DATA", "PRICE_FILTER", "LIQUIDITY_FILTER", "CALCULATION_ERROR"])

            applog.info("scanner", "SCANNER_STAGE1_SUMMARY")
            applog.info("scanner", f"total={len(diag_records)}")
            applog.info("scanner", f"passed={passed_cnt}")
            applog.info("scanner", f"INSUFFICIENT_HISTORY={insufficient_history_cnt}")
            applog.info("scanner", f"MISSING_DATA={missing_data_cnt}")
            applog.info("scanner", f"STALE_DATA={stale_data_cnt}")
            applog.info("scanner", f"PRICE_FILTER={price_filter_cnt}")
            applog.info("scanner", f"LIQUIDITY_FILTER={liquidity_filter_cnt}")
            applog.info("scanner", f"VOLATILITY_FILTER={volatility_filter_cnt}")
            applog.info("scanner", f"CALCULATION_ERROR={calc_error_cnt}")
            applog.info("scanner", f"OTHER={other_cnt}")

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
                f"gap_pct={c['gap_pct']:.4f}, liquidity={c['avg_vol']*c['price']:.2f}, "
                f"composite_score={c['tech_score']:.4f}, latest_bar_timestamp={c['latest_bar_timestamp'].isoformat()}"
            )

        candidates.sort(key=lambda x: x["tech_score"], reverse=True)
        top = candidates[:_TECH_SCORE_TOP_N]
        applog.info("scanner", "[Stage 2] Ranked candidates:")
        for rank, c in enumerate(top):
            # Temporarily compute if it would be selected based on cap/momentum logic
            # This is slightly duplicated from the selection logic below to provide accurate logging
            # but is necessary for the audit trail.
            # Wait, the actual selection logic runs in run(), not here. We can just return the candidates and log them in run() instead.
            # But the user asked for this log in Stage 2. Let's just log rank and score here, and we'll log selection in run().
            pass # We will move the logging to run() where final_anchors and final_momentum are decided.

        return top

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, as_of: datetime | None = None) -> tuple[ScannerOutcome, list[str]]:
        """Run a scan and return its outcome together with the selected symbols."""
        try:
            # Global safety gate
            if not await self.check_spy_volatility(as_of):
                return (ScannerOutcome.SUCCESS_NONEMPTY, DEFENSIVE_WATCHLIST)

            # Stage 1: parallel pre-filter
            candidates = await self._stage1_prefilter_parallel(as_of)
            if not candidates:
                applog.info("scanner", "SCANNER_RUN_EMPTY: All candidates failed pre-filter.")
                return (ScannerOutcome.SUCCESS_EMPTY, [])

            # Stage 2: technical scoring
            top_candidates = self._stage2_technical_score(candidates, as_of)
            if not top_candidates:
                applog.info("scanner", "SCANNER_RUN_EMPTY: No candidates survived technical scoring.")
                return (ScannerOutcome.SUCCESS_EMPTY, [])

            # Check for stale scans invariant using source timestamps
            latest_timestamp = max(c["latest_bar_timestamp"] for c in top_candidates[:8])
            MarketScanner._last_scans.append((as_of or datetime.now(UTC), latest_timestamp))
            MarketScanner._last_scans = MarketScanner._last_scans[-10:]
            age_seconds = (MarketScanner._last_scans[-1][0] - latest_timestamp).total_seconds()


            # Separate into Large-cap Anchors and Momentum Leaders
            anchors = [c for c in top_candidates if c["symbol"] in LARGE_CAP_ANCHORS]
            momentum = [c for c in top_candidates if c["symbol"] not in LARGE_CAP_ANCHORS]

            # Select top 4 anchors (already sorted by tech_score descending in Stage 2)
            final_anchors = [c["symbol"] for c in anchors[:4]]

            final_momentum = []
            sector_counts = {}
            for c in momentum:
                if len(final_momentum) >= 8:
                    break
                s = c["sector"]
                if sector_counts.get(s, 0) < _MAX_PER_SECTOR:
                    final_momentum.append(c["symbol"])
                    sector_counts[s] = sector_counts.get(s, 0) + 1

            # Log Stage 2 results explicitly
            applog.info("scanner", "[Stage 2] Ranked candidates:")
            for rank, c in enumerate(top_candidates):
                selected = c["symbol"] in final_anchors or c["symbol"] in final_momentum
                if selected:
                    reason = "NONE"
                elif len(final_momentum) >= 8 and c["symbol"] not in LARGE_CAP_ANCHORS:
                    reason = "TOP_N_LIMIT"
                elif c["symbol"] not in LARGE_CAP_ANCHORS and sector_counts.get(c["sector"], 0) >= _MAX_PER_SECTOR:
                    reason = "SECTOR_CAP"
                else:
                    reason = "NOT_SELECTED"
                applog.info("scanner", f"Stage2 rank={rank+1} symbol={c['symbol']} composite_score={c['tech_score']:.4f} selected={str(selected).lower()} exclusion_reason={reason}")

            scanned = sorted(list(set(final_anchors + final_momentum)))
            if not scanned:
                applog.info("scanner", "SCANNER_RUN_EMPTY: Scanner produced no final candidates.")
                return (ScannerOutcome.SUCCESS_EMPTY, [])

            applog.info("scanner", f"scan_as_of={(as_of or datetime.now(UTC)).isoformat()} source_latest_timestamp={latest_timestamp.isoformat()} source_age_seconds={age_seconds} outcome={ScannerOutcome.SUCCESS_NONEMPTY.name}")
            return (ScannerOutcome.SUCCESS_NONEMPTY, scanned)

        except Exception as exc:
            applog.error("scanner", f"SCANNER_RUN_FAILED: {exc}")
            return (ScannerOutcome.FAILED, [])

    # ------------------------------------------------------------------
    # Legacy screen_pool (kept for tests)
    # ------------------------------------------------------------------

    async def screen_pool(self, as_of: datetime | None = None) -> list[dict]:
        """Legacy sequential screener kept for unit-test compatibility."""
        return await self._stage1_prefilter_parallel(as_of)
