import sqlite3
import json
import pandas as pd
from datetime import datetime, timezone, timedelta

MASTER_DB = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
MASTER_BARS = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db"

def inspect_exact_bars_and_trade(symbol_to_check: str):
    conn_db = sqlite3.connect(MASTER_DB)
    cur = conn_db.cursor()
    cur.execute("SELECT id, symbol, pnl, exit_at, payload FROM trades WHERE exit_at LIKE '2026-08-03%' AND symbol = ?", (symbol_to_check,))
    rows = cur.fetchall()
    conn_db.close()

    conn_bars = sqlite3.connect(MASTER_BARS)
    
    for row in rows:
        t_id, sym, pnl, exit_at, payload_str = row
        p = json.loads(payload_str)
        entry_at = p.get("entry_at") or p.get("filled_at")
        entry_price = float(p.get("entry_price", p.get("avg_entry_price", 0)))
        exit_price = float(p.get("exit_price", 0))
        side = p.get("side", "long")
        qty = p.get("qty", 0)
        risk = p.get("initial_risk", 1.0)
        
        print("\n" + "=" * 100)
        print(f"EXACT RAW TRADE RECORD: {sym} (ID: {t_id})")
        print(f"Side: {side.upper()} | Qty: {qty} | Entry: ${entry_price:.2f} at {entry_at} | Master Exit: ${exit_price:.2f} at {exit_at} | Master PnL: ${pnl:.2f} | Risk/Share: ${risk:.2f}")
        print("=" * 100)

        dt = datetime.fromisoformat(entry_at.replace("Z", "+00:00"))
        start_ts = dt.timestamp() - 60
        end_ts = dt.timestamp() + 18000 # 5 hours

        df_bars = pd.read_sql("""
            SELECT ts, open, high, low, close, volume 
            FROM bars 
            WHERE symbol = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC
        """, conn_bars, params=(sym, start_ts, end_ts))

        df_bars["datetime_utc"] = pd.to_datetime(df_bars["ts"], unit="s", utc=True)
        df_bars["datetime_et"] = df_bars["datetime_utc"].dt.tz_convert("America/New_York")

        print(f"First 25 Exact 1-Minute Bars from Cache for {sym}:")
        print(f"{'TIME (ET)':<20} | {'OPEN':<8} {'HIGH':<8} {'LOW':<8} {'CLOSE':<8} | {'VOLUME':<10} | {'R-MULTIPLE':<10}")
        print("-" * 80)
        side_mult = 1.0 if side.lower() == "long" else -1.0
        for _, b in df_bars.head(25).iterrows():
            c = float(b["close"])
            r_val = (c - entry_price) * side_mult / float(risk)
            t_str = b["datetime_et"].strftime("%Y-%m-%d %H:%M:%S")
            print(f"{t_str:<20} | ${b['open']:<7.2f} ${b['high']:<7.2f} ${b['low']:<7.2f} ${b['close']:<7.2f} | {int(b['volume']):<10} | {r_val:>+8.2f} R")

    conn_bars.close()

if __name__ == "__main__":
    print("INSPECTING RAW DATA FOR NBIS:")
    inspect_exact_bars_and_trade("NBIS")
    print("\nINSPECTING RAW DATA FOR INTC (SHORT):")
    inspect_exact_bars_and_trade("INTC")
