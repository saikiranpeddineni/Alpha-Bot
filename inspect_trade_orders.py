import sqlite3
import json
import pandas as pd
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))
master_db = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
conn = sqlite3.connect(master_db)

cur = conn.cursor()

# Get all trades
df_trades = pd.read_sql("SELECT * FROM trades WHERE exit_at LIKE '2026-08-03%' ORDER BY exit_at ASC", conn)

# Get all orders
df_orders = pd.read_sql("SELECT * FROM orders WHERE ts LIKE '2026-08-03%' ORDER BY ts ASC", conn)

print(f"Total trades: {len(df_trades)}")

for idx, row in df_trades.iterrows():
    p = json.loads(row["payload"])
    sym = row["symbol"]
    entry_at = p.get("entry_at")
    exit_at = p.get("exit_at")
    
    # Find matching exit order in orders table
    matching_exit_orders = []
    for _, o in df_orders.iterrows():
        op = json.loads(o["payload"])
        if op.get("symbol") == sym:
            matching_exit_orders.append((o["ts"], op))
            
    print(f"\nTrade #{idx+1} {sym}: Entry={p.get('entry_price')} Exit={p.get('exit_price')} ExitReason={p.get('exit_reason')}")
    print(f"  EntryAt: {entry_at}")
    print(f"  ExitAt:  {exit_at}")
    print(f"  InitialStop: {p.get('initial_stop')} | InitialRisk: {p.get('initial_risk')} | PnL: ${row['pnl']:.2f}")
    print(f"  All orders for {sym} on this day:")
    for ots, op in matching_exit_orders:
        print(f"    - {ots}: side={op.get('side')} type={op.get('order_type')} price={op.get('price')} stop={op.get('stop_loss')} tp={op.get('take_profit')} strat={op.get('strategy')}")

conn.close()
