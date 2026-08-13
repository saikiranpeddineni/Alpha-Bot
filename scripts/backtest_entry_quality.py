"""
Backtest Entry Quality Script
=============================
Evaluates past trades using the new Entry Quality Engine to see what would have been
accepted or rejected by the new stricter thresholds.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np

# Path setup to import app modules
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.strategies.entry_quality import evaluate_entry_quality
from app.strategies.opportunity_state import classify_opportunity, OpportunityState
from app.config import RiskConfig


MASTER_ALPHADESK = REPO_ROOT / "data" / "alphadesk.db"
QA_BARS_DB       = REPO_ROOT / "data" / "bars_cache.db"

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute MACD, EMA9, and cumulative volume for VWAP on 5Min bars."""
    if df.empty:
        return df
        
    df = df.copy()
    
    # EMA 9
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    
    # MACD (12, 26, 9)
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_line"] = ema12 - ema26
    df["macd_signal"] = df["macd_line"].ewm(span=9, adjust=False).mean()
    df["macd_histogram"] = df["macd_line"] - df["macd_signal"]
    
    # VWAP (Intraday)
    # We group by date
    df["date"] = df.index.date
    df["typ_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["pv"] = df["typ_price"] * df["volume"]
    
    df["cum_pv"] = df.groupby("date")["pv"].cumsum()
    df["cum_vol"] = df.groupby("date")["volume"].cumsum()
    
    df["vwap"] = df["cum_pv"] / df["cum_vol"].replace(0, 1)
    
    # ATR 14
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['atr14'] = true_range.rolling(14).mean()
    
    return df

def main():
    print("=" * 80)
    print("  ENTRY QUALITY BACKTEST (5Min Bars)")
    print("=" * 80)

    if not MASTER_ALPHADESK.exists():
        print(f"ERROR: Master DB not found: {MASTER_ALPHADESK}")
        sys.exit(1)
    if not QA_BARS_DB.exists():
        print(f"ERROR: QA bars DB not found: {QA_BARS_DB}")
        sys.exit(1)
        
    mconn = sqlite3.connect(str(MASTER_ALPHADESK))
    df_raw = pd.read_sql(
        "SELECT id, mode, symbol, pnl, exit_at, payload FROM trades ORDER BY exit_at ASC;",
        mconn
    )
    mconn.close()

    records = []
    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
        records.append({
            "trade_id":    row["id"],
            "mode":        row["mode"],
            "symbol":      row["symbol"],
            "side":        p.get("side", "long"),
            "qty":         p.get("qty", 0),
            "strategy":    p.get("strategy", ""),
            "entry_price": p.get("entry_price"),
            "entry_at":    p.get("entry_at"),
            "legacy_pnl":  row["pnl"],
        })
        
    # Filter for August 13 paper bot trades
    trades = [
        r for r in records
        if r["entry_price"] and r["entry_at"]
        and r["mode"] == "paper"
        and str(r.get("strategy", "")) != "manual"
        and str(r.get("strategy", "")) != "alpaca"
        and str(r.get("entry_at", "")).startswith("2026-08-13")
    ]
    
    # Deduplicate
    seen = set()
    deduped_trades = []
    for r in trades:
        ea = str(r["entry_at"])[:16]
        key = (r["symbol"], ea, r["side"])
        if key not in seen:
            seen.add(key)
            deduped_trades.append(r)
            
    print(f"Evaluating {len(deduped_trades)} distinct bot entries from Aug 13...\n")
    
    bconn = sqlite3.connect(str(QA_BARS_DB))
    config = RiskConfig()
    
    accepted = 0
    rejected = 0
    results = []
    
    for trade in deduped_trades:
        symbol = trade["symbol"]
        side_str = trade["side"].lower()
        action = "ENTER_LONG" if "long" in side_str else "ENTER_SHORT"
        
        try:
            entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        except Exception:
            continue
            
        entry_epoch = entry_at.timestamp()
        
        # Load 5Min bars up to the entry time
        # We need enough history for MACD and VWAP (start of day)
        start_e = entry_epoch - 86400 * 2  # 2 days lookback
        
        df_bars = pd.read_sql(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? AND tf = '5Min' AND ts <= ? ORDER BY ts ASC",
            bconn,
            params=(symbol.upper(), int(entry_epoch))
        )
        
        if df_bars.empty:
            df_bars = pd.read_sql(
                "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? AND tf = '1Min' AND ts <= ? ORDER BY ts ASC",
                bconn,
                params=(symbol.upper(), int(entry_epoch))
            )
            # Resample 1Min to 5Min
            if not df_bars.empty:
                df_bars["dt"] = pd.to_datetime(df_bars["ts"], unit="s", utc=True)
                df_bars.set_index("dt", inplace=True)
                df_bars = df_bars.resample("5Min").agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum"
                }).dropna()
                
        else:
            df_bars["dt"] = pd.to_datetime(df_bars["ts"], unit="s", utc=True)
            df_bars.set_index("dt", inplace=True)
            
        if df_bars.empty or len(df_bars) < 26:
            results.append({"symbol": symbol, "time": entry_at, "status": "ERROR_NO_BARS"})
            continue
            
        df_ind = compute_indicators(df_bars)
        last_bar = df_ind.iloc[-1]
        
        # Estimate parameters for classify_opportunity
        close_price = trade["entry_price"]
        vwap_val = last_bar["vwap"]
        atr_val = last_bar["atr14"]
        atr_pct = (atr_val / close_price * 100.0) if close_price else 1.0
        extension_atr = abs(close_price - vwap_val) / max(atr_val, 1e-9)
        
        # Estimate vol_z (basic)
        vol_sma20 = df_ind["volume"].rolling(20).mean().iloc[-1]
        vol_std20 = df_ind["volume"].rolling(20).std().iloc[-1]
        vol_z = (last_bar["volume"] - vol_sma20) / max(vol_std20, 1.0) if pd.notna(vol_std20) else 0.0
        
        opp_state = classify_opportunity(
            atr_pct=atr_pct,
            gap_pct=0.0,
            news_catalyst=False,
            volume_z=vol_z,
            extension_atr=extension_atr,
            trend_score=75.0,
        )
        
        # Determine threshold
        if opp_state == OpportunityState.EVENT:
            threshold = config.event_entry_quality_threshold
        elif opp_state == OpportunityState.PRICE_DISCOVERY:
            threshold = config.price_discovery_entry_quality_threshold
        else:
            threshold = config.normal_entry_quality_threshold
            
        # Evaluate Entry Quality
        eq_res = evaluate_entry_quality(
            side=side_str,
            close=close_price,
            vwap=vwap_val,
            atr=atr_val,
            df=df_ind,
            vol_z=vol_z,
            catalyst_score=80.0 if opp_state == OpportunityState.EVENT else 50.0,
            trend_score=75.0
        )
        
        score = eq_res.score
        status = "ACCEPTED" if score >= threshold else "REJECTED"
        
        if status == "ACCEPTED":
            accepted += 1
        else:
            rejected += 1
            
        print(f"{symbol:<6} | {entry_at.strftime('%H:%M')} | {opp_state.name:<15} | Score: {score:>5.1f} | Thr: {threshold:>4.1f} | {status} | PnL: ${trade['legacy_pnl']:.2f}")
        
    bconn.close()
    
    print("-" * 80)
    print(f"Total Evaluated : {accepted + rejected}")
    print(f"Accepted        : {accepted}")
    print(f"Rejected        : {rejected}")
    print("=" * 80)
    
if __name__ == "__main__":
    main()
