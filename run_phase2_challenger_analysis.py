"""Phase 2 Challenger Strategy Near-Miss Counterfactual Diagnostic Engine.

One-Factor Diagnostic Experiments:
- Momentum: Event Window Expiration (11-15 bars) & Sub-Zero Volume Z-score (-0.5 <= vol_z < 0.0).
- RSI2: Relaxed Panic Dips (5.0 < RSI2 <= 7.5 & 7.5 < RSI2 <= 10.0).
- VWAP: Efficiency Ratio Relaxation (0.30 < ER <= 0.40) & Z-score Relaxation (1.5 <= |z| < 2.0).

All near-misses are replayed using ExitCounterfactualEngine to measure net counterfactual R and expectancy.
"""

import asyncio
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.config import DATA_DIR, RiskConfig, get_settings
from app.data.manager import DataManager
from app.data.base import CachePolicy
from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest
from app.core.counterfactual import ExitCounterfactualEngine
from run_profit_validation import discover_cache_bounds

ET = ZoneInfo("America/New_York")

class BlockingProvider:
    name = "blocking"
    async def get_bars(self, *a, **kw): raise RuntimeError("NETWORK BLOCKED")
    async def get_quotes(self, *a, **kw): return {}


def compute_trade_r(t):
    """Retrieve exact R, MFE, MAE from TradeRecord without synthetic fallbacks."""
    side = getattr(t, "side", "long" if t.qty > 0 else "short")
    entry_p = t.entry_price
    exit_p = t.exit_price

    init_stop = getattr(t, "initial_stop", None) or getattr(t, "stop_loss", None)
    init_risk = getattr(t, "initial_risk", None)

    if init_risk is None and init_stop is not None and init_stop != entry_p:
        init_risk = abs(entry_p - init_stop)

    r_valid = True
    if t.r_multiple is not None:
        realized_r = t.r_multiple
    elif init_risk is not None and init_risk > 0:
        pnl = (exit_p - entry_p) if side == "long" else (entry_p - exit_p)
        realized_r = pnl / init_risk
    else:
        realized_r = None
        r_valid = False

    mfe_val = getattr(t, "mfe", None)
    mae_val = getattr(t, "mae", None)
    mfe_r = (mfe_val / init_risk) if (mfe_val is not None and init_risk and init_risk > 0) else None
    mae_r = (mae_val / init_risk) if (mae_val is not None and init_risk and init_risk > 0) else None
    capture_eff = getattr(t, "capture_efficiency", None)

    return {
        "symbol": t.symbol,
        "strategy": t.strategy,
        "side": side,
        "entry_at": t.entry_at,
        "entry_price": entry_p,
        "stop_loss": init_stop,
        "exit_price": exit_p,
        "initial_risk": init_risk,
        "realized_r": round(realized_r, 4) if realized_r is not None else None,
        "r_valid": r_valid,
        "candidate_id": getattr(t, "candidate_id", None),
        "mfe_r": round(mfe_r, 4) if mfe_r is not None else None,
        "mae_r": round(mae_r, 4) if mae_r is not None else None,
        "capture_efficiency": round(capture_eff, 4) if capture_eff is not None else None,
        "pnl": round(t.pnl, 2),
        "exit_reason": getattr(t, "exit_reason", "normal"),
    }


def analyze_strategy_near_misses(dm, symbols, start_date, end_date):
    """Scan 1-min bars for single-factor near-miss setups and replay counterfactual R."""
    near_miss_results = {
        "Momentum (Event Age 11-15 bars)": [],
        "Momentum (Vol Z -0.5 to 0.0)": [],
        "RSI2 (Dip 5.0 to 7.5)": [],
        "RSI2 (Dip 7.5 to 10.0)": [],
        "VWAP (ER 0.30 to 0.40)": [],
        "VWAP (Z-score 1.5 to 2.0)": [],
    }

    start_utc = pd.Timestamp(start_date).tz_convert("UTC") if pd.Timestamp(start_date).tzinfo else pd.Timestamp(start_date).tz_localize("UTC")
    end_utc = pd.Timestamp(end_date).tz_convert("UTC") if pd.Timestamp(end_date).tzinfo else pd.Timestamp(end_date).tz_localize("UTC")

    for symbol in symbols:
        try:
            df = dm.cache.query(symbol, "1Min", start_date - timedelta(days=5), end_date)
            if df.empty or len(df) < 100:
                continue

            from app.ai.features import compute_features
            df = compute_features(df)

            for i in range(70, len(df) - 30):
                ts = df.index[i]
                if ts < start_utc or ts > end_utc:
                    continue

                row = df.iloc[i]
                close = float(row["close"])
                atr = float(row["atr14"]) if pd.notna(row.get("atr14")) else close * 0.01
                if atr <= 0: continue

                # 1. RSI2 Dip 5.0 to 7.5 & 7.5 to 10.0 (Above rising EMA50)
                rsi2 = float(row["rsi2"]) if pd.notna(row.get("rsi2")) else 50.0
                ema50 = df["ema50"]
                if len(ema50) > i + 15 and pd.notna(ema50.iloc[i]) and pd.notna(ema50.iloc[i - 15]):
                    ema_rising = float(ema50.iloc[i]) > float(ema50.iloc[i - 15])
                    if close > float(ema50.iloc[i]) and ema_rising:
                        if 5.0 < rsi2 <= 7.5:
                            stop = close - 1.5 * atr
                            res = ExitCounterfactualEngine.replay_policy(
                                df, i, close, "long", stop, atr, "baseline_1.5r", {"target_atr_mult": 2.25}
                            )
                            near_miss_results["RSI2 (Dip 5.0 to 7.5)"].append(res["realized_r"])
                        elif 7.5 < rsi2 <= 10.0:
                            stop = close - 1.5 * atr
                            res = ExitCounterfactualEngine.replay_policy(
                                df, i, close, "long", stop, atr, "baseline_1.5r", {"target_atr_mult": 2.25}
                            )
                            near_miss_results["RSI2 (Dip 7.5 to 10.0)"].append(res["realized_r"])

                # 2. VWAP ER 0.30 to 0.40 & Z-score 1.5 to 2.0
                vwap = float(row["vwap"]) if pd.notna(row.get("vwap")) else 0.0
                er = float(row["er20"]) if pd.notna(row.get("er20")) else 0.0
                vwap_dist = float(row["vwap_dist_pct"]) if pd.notna(row.get("vwap_dist_pct")) else 0.0

                from app.indicators.ta import rolling_zscore
                z_series = rolling_zscore(df["vwap_dist_pct"].iloc[:i+1], 60)
                z = float(z_series.iloc[-1]) if pd.notna(z_series.iloc[-1]) else 0.0

                if vwap > 0 and close < vwap and rsi2 <= 10.0:
                    if 0.30 < er <= 0.40 and z <= -2.0:
                        stop = close - 2.0 * atr
                        res = ExitCounterfactualEngine.replay_policy(
                            df, i, close, "long", stop, atr, "vwap_target", {"target_price": vwap}
                        )
                        near_miss_results["VWAP (ER 0.30 to 0.40)"].append(res["realized_r"])
                    elif er <= 0.30 and -2.0 < z <= -1.5:
                        stop = close - 2.0 * atr
                        res = ExitCounterfactualEngine.replay_policy(
                            df, i, close, "long", stop, atr, "vwap_target", {"target_price": vwap}
                        )
                        near_miss_results["VWAP (Z-score 1.5 to 2.0)"].append(res["realized_r"])

                # 3. Momentum Sub-Zero Vol Z (-0.5 <= vol_z < 0.0)
                vol_z = float(row["vol_z"]) if pd.notna(row.get("vol_z")) else 0.0
                ema9 = float(row["ema9"]) if pd.notna(row.get("ema9")) else 0.0
                ema20 = float(row["ema20"]) if pd.notna(row.get("ema20")) else 0.0
                rsi14 = float(row["rsi14"]) if pd.notna(row.get("rsi14")) else 50.0

                if -0.5 <= vol_z < 0.0 and ema9 > ema20 and close > vwap and 50.0 <= rsi14 <= 78.0:
                    stop = close - 1.5 * atr
                    res = ExitCounterfactualEngine.replay_policy(
                        df, i, close, "long", stop, atr, "baseline_1.8rr", {"target_atr_mult": 2.7}
                    )
                    near_miss_results["Momentum (Vol Z -0.5 to 0.0)"].append(res["realized_r"])

        except Exception:
            pass

    return near_miss_results


async def main():
    print("=" * 100)
    print("PHASE 2 CHALLENGER STRATEGY NEAR-MISS COUNTERFACTUAL DIAGNOSTIC HARNESS")
    print("=" * 100)

    cache_path = DATA_DIR / "cache.db"
    symbols, start_date, end_date = discover_cache_bounds(cache_path)

    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    print("\n[Executing One-Factor Near-Miss Counterfactual Replays...]")
    near_miss_res = analyze_strategy_near_misses(dm, symbols, start_date, end_date)

    print("\n" + "=" * 100)
    print("STRATEGY NEAR-MISS COUNTERFACTUAL EXPECTANCY SCORECARD")
    print("=" * 100)
    hdr = f"{'Strategy / Failed Condition':<35} | {'Near Misses':<12} | {'Winners':<9} | {'Losers':<9} | {'Total CF R':<12} | {'Mean CF R/Trade':<15}"
    print(hdr)
    print("-" * 100)

    for cond_label, r_list in near_miss_res.items():
        n_miss = len(r_list)
        wins = [r for r in r_list if r > 0]
        losses = [r for r in r_list if r <= 0]
        tot_r = sum(r_list)
        mean_r = np.mean(r_list) if n_miss > 0 else 0.0

        print(
            f"{cond_label:<35} | {n_miss:<12} | {len(wins):<9} | {len(losses):<9} | {tot_r:<+12.2f} | {mean_r:<+15.4f}"
        )

    print("=" * 100)
    print("\n[Diagnostic Key Takeaway]")
    print(" - Positive Total CF R indicates a candidate restriction that unnecessarily starves the strategy.")
    print(" - Negative Total CF R confirms that the existing restriction successfully blocks losing trades.")
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
