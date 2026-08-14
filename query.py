import sqlite3
import pandas as pd
import json

conn = sqlite3.connect('data/alphadesk.db')
df = pd.read_sql_query("SELECT * FROM orders ORDER BY id DESC", conn)
sndk_orders = []
for i in range(len(df)):
    try:
        payload = json.loads(df['payload'].iloc[i])
        if payload.get('symbol') == 'SNDK':
            sndk_orders.append(payload)
    except:
        pass

for payload in sndk_orders[:5]:
    print("--- Order ---")
    for k, v in payload.items():
        if 'time' in k or 'at' in k:
            print(f"{k}: {v}")
    print(f"Action: {payload.get('action')}, Qty: {payload.get('qty')}")
