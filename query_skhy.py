import sqlite3
import pandas as pd
import json

conn = sqlite3.connect('data/alphadesk.db')
df = pd.read_sql_query('SELECT * FROM trades WHERE symbol="SKHY" ORDER BY exit_at DESC LIMIT 1', conn)
if len(df) > 0:
    payload = json.loads(df['payload'].iloc[0])
    print("--- TRADE PAYLOAD ---")
    for k, v in payload.items():
        print(f"{k}: {v}")

df_orders = pd.read_sql_query("SELECT * FROM orders ORDER BY id DESC", conn)
skhy_orders = []
for i in range(len(df_orders)):
    try:
        p = json.loads(df_orders['payload'].iloc[i])
        if p.get('symbol') == 'SKHY':
            skhy_orders.append((df_orders['id'].iloc[i], p))
    except:
        pass

for order_id, p in skhy_orders[:3]:
    print(f"--- ORDER {order_id} ---")
    for k, v in p.items():
        if 'time' in k or 'at' in k or 'price' in k or k in ['action', 'qty', 'strategy', 'status', 'reason']:
            print(f"{k}: {v}")
