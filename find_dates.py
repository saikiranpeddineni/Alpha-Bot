import sqlite3
import pandas as pd
import os
import glob

print("Checking databases for max timestamp and any NBIS/COHR events...")

for db in glob.glob("*.db"):
    print(f"\n--- {db} ---")
    try:
        conn = sqlite3.connect(db)
        tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)['name'].tolist()
        
        if 'orders' in tables:
            max_ts = pd.read_sql("SELECT MAX(timestamp) as m FROM orders", conn)['m'][0]
            print(f"Max order timestamp: {max_ts}")
            
            df = pd.read_sql("SELECT * FROM orders WHERE symbol IN ('NBIS', 'COHR') ORDER BY timestamp DESC LIMIT 5", conn)
            if not df.empty:
                print(f"Found NBIS/COHR orders:")
                print(df[['timestamp', 'symbol', 'action', 'qty', 'status']].to_string())
                
        if 'trades' in tables:
            max_ts = pd.read_sql("SELECT MAX(timestamp) as m FROM trades", conn)['m'][0]
            print(f"Max trade timestamp: {max_ts}")
            
            df = pd.read_sql("SELECT * FROM trades WHERE symbol IN ('NBIS', 'COHR') ORDER BY timestamp DESC LIMIT 5", conn)
            if not df.empty:
                print(f"Found NBIS/COHR trades:")
                print(df[['timestamp', 'symbol', 'pnl']].to_string())
                
        if 'policy_rejections' in tables:
            try:
                # the column might be 'candidate_id' and not 'symbol'
                df = pd.read_sql("SELECT * FROM policy_rejections WHERE candidate_id LIKE '%NBIS%' OR candidate_id LIKE '%COHR%' ORDER BY timestamp DESC LIMIT 5", conn)
                if not df.empty:
                    print(f"Found NBIS/COHR policy rejections:")
                    print(df[['timestamp', 'candidate_id', 'reason']].to_string())
            except Exception as e:
                pass
    except Exception as e:
        print(f"Error reading {db}: {e}")
