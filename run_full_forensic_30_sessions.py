import sqlite3
import pandas as pd
import json
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

# Connect to databases
alphadesk_db = r"c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
bars_db = r"c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db"

conn = sqlite3.connect(alphadesk_db)
df_trades_raw = pd.read_sql("SELECT * FROM trades ORDER BY id ASC", conn)
conn.close()

conn_bars = sqlite3.connect(bars_db)

trade_list = []
for idx, row in df_trades_raw.iterrows():
    p = json.loads(row["payload"]) if row.get("payload") else {}
    entry_at_str = p.get("entry_at") or row.get("exit_at")
    exit_at_str = row.get("exit_at") or p.get("exit_at")
    
    entry_dt = pd.to_datetime(entry_at_str)
    exit_dt = pd.to_datetime(exit_at_str)
    
    side = str(p.get("side", "long")).lower()
    qty = float(p.get("qty") or 0)
    entry_px = float(p.get("entry_price") or 0)
    exit_px = float(p.get("exit_price") or 0)
    pnl = float(row.get("pnl") or 0)
    
    init_stop = float(p.get("initial_stop")) if p.get("initial_stop") is not None else None
    init_risk = float(p.get("initial_risk")) if p.get("initial_risk") is not None else None
    
    # Per-share risk
    if init_risk is not None and init_risk > 0.0001:
        risk_per_share = init_risk
    elif entry_px and init_stop:
        risk_per_share = abs(entry_px - init_stop)
    else:
        risk_per_share = max(0.01, entry_px * 0.01) # fallback 1%
        
    initial_risk_dollars = risk_per_share * qty if qty > 0 else (risk_per_share * 100)

    trade_list.append({
        "id": row["id"],
        "symbol": str(row["symbol"]).upper(),
        "strategy": p.get("strategy") or "unknown",
        "side": side,
        "qty": qty,
        "entry_px": entry_px,
        "exit_px": exit_px,
        "init_stop": init_stop,
        "risk_per_share": risk_per_share,
        "initial_risk_dollars": initial_risk_dollars,
        "pnl": pnl,
        "r_multiple_recorded": float(p.get("r_multiple")) if p.get("r_multiple") is not None else None,
        "exit_reason": str(p.get("exit_reason") or ""),
        "entry_dt": entry_dt,
        "exit_dt": exit_dt,
        "date": entry_dt.date() if pd.notnull(entry_dt) else None,
        "payload_mfe": float(p.get("mfe")) if p.get("mfe") is not None else None,
        "payload_mae": float(p.get("mae")) if p.get("mae") is not None else None,
    })

df = pd.DataFrame(trade_list)
unique_dates = sorted(df["date"].dropna().unique())
last_30_dates = unique_dates[-30:]

df_30 = df[df["date"].isin(last_30_dates)].copy().reset_index(drop=True)
print(f"Total trades in 30 sessions: {len(df_30)}")

# Detailed forensic evaluation
forensic_trades = []

for idx, t in df_30.iterrows():
    sym = t["symbol"]
    side = t["side"]
    entry_px = t["entry_px"]
    exit_px = t["exit_px"]
    rps = t["risk_per_share"]
    qty = t["qty"]
    pnl = t["pnl"]
    
    # Fetch 1-min bars for the trade duration
    # include a 1-min buffer before/after
    ts_start = int(t["entry_dt"].timestamp()) - 60
    ts_end = int(t["exit_dt"].timestamp()) + 60
    
    query = """
        SELECT ts, open, high, low, close, volume 
        FROM bars 
        WHERE symbol = ? AND tf = '1Min' AND ts >= ? AND ts <= ?
        ORDER BY ts ASC
    """
    bars_cur = conn_bars.execute(query, (sym, ts_start, ts_end)).fetchall()
    
    # Filter bars that are strictly within trade window or closest
    bars_list = []
    trade_start_ts = int(t["entry_dt"].timestamp())
    trade_end_ts = int(t["exit_dt"].timestamp())
    
    for b in bars_cur:
        # We consider bars starting from trade_start_ts - 59 to trade_end_ts + 59
        if (b[0] + 60) >= trade_start_ts and b[0] <= (trade_end_ts + 30):
            bars_list.append({
                "ts": b[0],
                "open": b[1],
                "high": b[2],
                "low": b[3],
                "close": b[4],
                "volume": b[5],
                "dt": datetime.fromtimestamp(b[0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            })
            
    # If no bars in buffer, see if any bars returned at all
    if not bars_list and bars_cur:
        bars_list = [{
            "ts": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5],
            "dt": datetime.fromtimestamp(b[0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        } for b in bars_cur]

    # Bar by bar path analysis
    max_favorable_price = entry_px
    max_adverse_price = entry_px
    peak_r = 0.0
    trough_r = 0.0
    peak_bar_idx = 0
    total_bars = len(bars_list)
    
    bars_progression = []
    for b_idx, b in enumerate(bars_list):
        if side == "long":
            bar_peak_r = (b["high"] - entry_px) / rps if rps > 0 else 0.0
            bar_trough_r = (b["low"] - entry_px) / rps if rps > 0 else 0.0
            bar_close_r = (b["close"] - entry_px) / rps if rps > 0 else 0.0
            if b["high"] > max_favorable_price:
                max_favorable_price = b["high"]
            if b["low"] < max_adverse_price:
                max_adverse_price = b["low"]
        else:
            bar_peak_r = (entry_px - b["low"]) / rps if rps > 0 else 0.0
            bar_trough_r = (entry_px - b["high"]) / rps if rps > 0 else 0.0
            bar_close_r = (entry_px - b["close"]) / rps if rps > 0 else 0.0
            if b["low"] < max_favorable_price:
                max_favorable_price = b["low"]
            if b["high"] > max_adverse_price:
                max_adverse_price = b["high"]
                
        if bar_peak_r > peak_r:
            peak_r = bar_peak_r
            peak_bar_idx = b_idx
            
        if bar_trough_r < trough_r:
            trough_r = bar_trough_r
            
        bars_progression.append({
            "bar": b_idx,
            "dt": b["dt"],
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "vol": b["volume"],
            "peak_r_on_bar": round(bar_peak_r, 3),
            "close_r_on_bar": round(bar_close_r, 3),
            "trough_r_on_bar": round(bar_trough_r, 3)
        })

    # Realized Exit R
    if side == "long":
        exit_r = (exit_px - entry_px) / rps if rps > 0 else 0.0
    else:
        exit_r = (entry_px - exit_px) / rps if rps > 0 else 0.0

    # Ensure peak_r is at least exit_r if exit was positive
    if exit_r > peak_r:
        peak_r = exit_r

    r_surrendered = max(0.0, peak_r - exit_r)
    peak_dollars = peak_r * (rps * qty) if qty > 0 else 0.0
    dollars_surrendered = max(0.0, peak_dollars - pnl)
    
    # Classification
    classification = "UNKNOWN"
    if peak_r >= 1.0 and pnl < 0:
        classification = "WINNER_TURNED_LOSER" # Reached >= 1R but closed negative!
    elif peak_r >= 1.5 and exit_r < 0.3:
        classification = "SEVERE_PROFIT_GIVEBACK" # Reached >= 1.5R but surrendered almost everything
    elif peak_r >= 1.0 and exit_r < (0.5 * peak_r):
        classification = "MODERATE_GIVEBACK" # Surrendered > 50% of peak R
    elif peak_r < 0.5 and pnl <= 0:
        classification = "IMMEDIATE_FAILURE" # Never went in our favor
    elif exit_r >= 0.8 * peak_r and exit_r > 0:
        classification = "CLEAN_CAPTURE" # Captured most of peak move
    elif pnl > 0:
        classification = "MODEST_WIN"
    else:
        classification = "NORMAL_LOSS"

    forensic_trades.append({
        "id": t["id"],
        "date": str(t["date"]),
        "symbol": sym,
        "strategy": t["strategy"],
        "side": side,
        "qty": qty,
        "entry_px": entry_px,
        "exit_px": exit_px,
        "risk_per_share": round(rps, 4),
        "init_risk_dollars": round(t["initial_risk_dollars"], 2),
        "entry_at": str(t["entry_dt"]),
        "exit_at": str(t["exit_dt"]),
        "pnl": round(pnl, 2),
        "exit_r": round(exit_r, 3),
        "peak_r": round(peak_r, 3),
        "trough_r": round(trough_r, 3),
        "r_surrendered": round(r_surrendered, 3),
        "peak_dollars": round(peak_dollars, 2),
        "dollars_surrendered": round(dollars_surrendered, 2),
        "total_bars": total_bars,
        "peak_bar_idx": peak_bar_idx,
        "peak_bar_ratio": round(peak_bar_idx / max(1, total_bars - 1), 2) if total_bars > 1 else 0.0,
        "exit_reason": t["exit_reason"],
        "classification": classification,
        "bars": bars_progression
    })

conn_bars.close()

# Save detailed json report with bars for all trades
out_json_path = r"c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\reports\forensic_30_sessions_trades_with_bars.json"
Path(out_json_path).parent.mkdir(parents=True, exist_ok=True)

# Separate without bars for lightweight csv
df_forensic = pd.DataFrame([{k: v for k, v in ft.items() if k != "bars"} for ft in forensic_trades])
out_csv_path = r"c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\reports\forensic_30_sessions_summary.csv"
df_forensic.to_csv(out_csv_path, index=False)

with open(out_json_path, "w") as f:
    json.dump(forensic_trades, f, indent=2)

print(f"Saved forensic analysis of {len(forensic_trades)} trades to {out_json_path} and {out_csv_path}")

# Print high-level metrics
print("\n" + "="*80)
print("HIGH-LEVEL FORENSIC SUMMARY (LAST 30 SESSIONS)")
print("="*80)
print(f"Total Trades Analyzed: {len(df_forensic)}")
print(f"Total Realized PnL: ${df_forensic['pnl'].sum():,.2f}")
print(f"Total Potential Peak Dollars: ${df_forensic['peak_dollars'].sum():,.2f}")
print(f"Total Dollars Surrendered: ${df_forensic['dollars_surrendered'].sum():,.2f}")

print("\n--- CLASSIFICATION BREAKDOWN ---")
print(df_forensic["classification"].value_counts())

print("\n--- PEAK R DISTRIBUTION ---")
print(f"Trades reaching >= 0.5R: {(df_forensic['peak_r'] >= 0.5).sum()} ({(df_forensic['peak_r'] >= 0.5).mean()*100:.1f}%)")
print(f"Trades reaching >= 1.0R: {(df_forensic['peak_r'] >= 1.0).sum()} ({(df_forensic['peak_r'] >= 1.0).mean()*100:.1f}%)")
print(f"Trades reaching >= 1.5R: {(df_forensic['peak_r'] >= 1.5).sum()} ({(df_forensic['peak_r'] >= 1.5).mean()*100:.1f}%)")
print(f"Trades reaching >= 2.0R: {(df_forensic['peak_r'] >= 2.0).sum()} ({(df_forensic['peak_r'] >= 2.0).mean()*100:.1f}%)")
print(f"Trades reaching >= 3.0R: {(df_forensic['peak_r'] >= 3.0).sum()} ({(df_forensic['peak_r'] >= 3.0).mean()*100:.1f}%)")
print(f"Trades reaching >= 5.0R: {(df_forensic['peak_r'] >= 5.0).sum()} ({(df_forensic['peak_r'] >= 5.0).mean()*100:.1f}%)")

print("\n--- THE TRAGIC PARADOX: WINNERS TURNED INTO LOSSES ---")
win_to_loss = df_forensic[(df_forensic["peak_r"] >= 1.0) & (df_forensic["pnl"] <= 0)]
print(f"Trades that achieved >= 1.0R peak profit but ended in a LOSS/SCRATCH: {len(win_to_loss)}")
print(f"  Total PnL of these trades: ${win_to_loss['pnl'].sum():,.2f}")
print(f"  Peak Dollars they had on the table: ${win_to_loss['peak_dollars'].sum():,.2f}")
print(f"  Dollars completely given back: ${win_to_loss['dollars_surrendered'].sum():,.2f}")

win2_to_loss = df_forensic[(df_forensic["peak_r"] >= 2.0) & (df_forensic["pnl"] <= 0)]
print(f"\nTrades that achieved >= 2.0R peak profit but ended in a LOSS/SCRATCH: {len(win2_to_loss)}")
print(f"  Total PnL of these trades: ${win2_to_loss['pnl'].sum():,.2f}")
print(f"  Peak Dollars they had on the table: ${win2_to_loss['peak_dollars'].sum():,.2f}")

print("\n--- BY STRATEGY: SURRENDERED DOLLARS & R ---")
strat_summary = df_forensic.groupby("strategy").agg(
    trades=("id", "count"),
    realized_pnl=("pnl", "sum"),
    peak_dollars=("peak_dollars", "sum"),
    dollars_surrendered=("dollars_surrendered", "sum"),
    avg_peak_r=("peak_r", "mean"),
    avg_exit_r=("exit_r", "mean"),
    winners_turned_losers=("classification", lambda s: (s == "WINNER_TURNED_LOSER").sum())
)
print(strat_summary)

print("\n--- EXIT REASONS FOR WINNER_TURNED_LOSER TRADES ---")
print(win_to_loss["exit_reason"].value_counts().head(10))
