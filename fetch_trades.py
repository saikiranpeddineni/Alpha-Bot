import sqlite3
import json
import pandas as pd
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))

master_db = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
conn = sqlite3.connect(master_db)

cur = conn.cursor()
cur.execute("PRAGMA table_info(orders)")
order_cols = [c[1] for c in cur.fetchall()]
print("Orders columns:", order_cols)

# Check trades
df = pd.read_sql("SELECT * FROM trades", conn)

trades_0803 = []
for idx, row in df.iterrows():
    payload = {}
    if row.get("payload"):
        try:
            payload = json.loads(row["payload"])
        except Exception:
            pass
    exit_at = str(row.get("exit_at") or payload.get("exit_at") or "")
    entry_at = str(payload.get("entry_at") or "")
    if "2026-08-03" in exit_at or "2026-08-03" in entry_at:
        trades_0803.append((row, payload))

print(f"Total completed trades on 2026-08-03: {len(trades_0803)}")

for i, (r, p) in enumerate(trades_0803, 1):
    entry_at_str = p.get("entry_at") or ""
    exit_at_str = p.get("exit_at") or r.get("exit_at") or ""
    
    entry_dt = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00")) if entry_at_str else None
    exit_dt = datetime.fromisoformat(exit_at_str.replace("Z", "+00:00")) if exit_at_str else None
    
    entry_et = entry_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET") if entry_dt else "N/A"
    exit_et = exit_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET") if exit_dt else "N/A"
    
    sym = r.get("symbol")
    strat = p.get("strategy") or r.get("strategy")
    side = p.get("side")
    qty = p.get("qty")
    entry_px = p.get("entry_price")
    exit_px = p.get("exit_price")
    sl = p.get("initial_stop")
    risk = p.get("initial_risk")
    tp = p.get("take_profit") or p.get("target_price") or p.get("tp")
    pnl = r.get("pnl")
    exit_reason = p.get("exit_reason")
    
    print(f"\nTrade #{i}: {sym} | Strat: {strat} | Side: {side} | Qty: {qty}")
    print(f"  Entry: {entry_px} at {entry_at_str} ({entry_et})")
    print(f"  Exit:  {exit_px} at {exit_at_str} ({exit_et})")
    print(f"  SL (Initial Stop): {sl} | Initial Risk: {risk} | TP (Take Profit): {tp}")
    print(f"  PnL: ${pnl:.2f} | Reason: {exit_reason}")

conn.close()
