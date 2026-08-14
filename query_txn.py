import sqlite3
import pandas as pd
import json

conn = sqlite3.connect('data/alphadesk.db')

print("=== TRADES ===")
df = pd.read_sql_query('SELECT * FROM trades WHERE symbol="TXN" ORDER BY exit_at DESC LIMIT 2', conn)
for i in range(len(df)):
    payload = json.loads(df['payload'].iloc[i])
    print(f"Trade {i}: Entry {payload.get('entry_at')} | Exit {payload.get('exit_at')}")
    print(f"   Entry Price: {payload.get('entry_price')} | Exit Price: {payload.get('exit_price')}")

print("\n=== ORDERS ===")
df_orders = pd.read_sql_query("SELECT * FROM orders ORDER BY id DESC", conn)
txn_orders = []
for i in range(len(df_orders)):
    try:
        p = json.loads(df_orders['payload'].iloc[i])
        if p.get('symbol') == 'TXN':
            txn_orders.append(p)
    except:
        pass

for p in txn_orders[:3]:
    print(f"Order: {p.get('side')} {p.get('qty')} | Status: {p.get('status')}")
    print(f"   Submitted At: {p.get('submitted_at')}")
    print(f"   Filled At:    {p.get('filled_at')}")
    if p.get('signal_timestamp_utc'):
        print(f"   Signal At:    {p.get('signal_timestamp_utc')}")
    if p.get('decision_bar_timestamp_utc'):
        print(f"   Decision At:  {p.get('decision_bar_timestamp_utc')}")
    print(f"   Reason:       {p.get('reason')}")
