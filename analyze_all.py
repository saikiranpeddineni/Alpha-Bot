import sqlite3
import pandas as pd
import json
import datetime

symbols = ['SKHY', 'DDOG', 'GOOGL', 'NBIS', 'PLTR']

def analyze_all():
    conn = sqlite3.connect('data/alphadesk.db')
    
    for sym in symbols:
        print(f"\n==================== {sym} ====================")
        print("=== TRADES ===")
        trades_df = pd.read_sql_query("SELECT * FROM trades", conn)
        for _, row in trades_df.iterrows():
            try:
                payload = json.loads(row['payload'])
                if payload.get('symbol') == sym and '2026-08-06' in payload.get('entry_at', ''):
                    print(json.dumps(payload, indent=2))
            except Exception as e:
                pass
                
        print("\n=== ORDERS ===")
        orders_df = pd.read_sql_query("SELECT * FROM orders", conn)
        for _, row in orders_df.iterrows():
            try:
                payload = json.loads(row['payload'])
                if payload.get('symbol') == sym and '2026-08-06' in str(row['ts']):
                    print(f"[{row['ts']}] {payload.get('side').upper()} {payload.get('qty')} @ {payload.get('filled_avg_price')} (Status: {payload.get('status')}) | Reason: {payload.get('reason', 'N/A')} | Strategy: {payload.get('strategy', 'N/A')}")
            except Exception as e:
                pass
                
        print("\n=== SHADOW LEDGER ===")
        shadow_df = pd.read_sql_query(f"SELECT * FROM shadow_ledger WHERE payload LIKE '%{sym}%' AND ts LIKE '%2026-08-06%' ORDER BY ts DESC LIMIT 1", conn)
        for _, row in shadow_df.iterrows():
            try:
                payload = json.loads(row['payload'])
                if payload.get('symbol') == sym:
                    print(f"[{row['ts']}] Strategy: {payload.get('strategy_name', payload.get('strategy'))}")
                    print(json.dumps(payload, indent=2))
            except Exception as e:
                print(f"Error parsing shadow payload: {e}")
                
    conn.close()

if __name__ == "__main__":
    analyze_all()
