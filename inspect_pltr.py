import sqlite3
import json
import os

paths = ["alpha.db", "app.db", "data/alphadesk.db", "data/alphadesk.old.db", r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"]

for p in paths:
    if not os.path.exists(p):
        continue
    print(f"\n--- Checking DB: {p} ---")
    try:
        conn = sqlite3.connect(p)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        if cur.fetchone():
            cur.execute("SELECT symbol, payload FROM trades WHERE symbol = 'PLTR'")
            rows = cur.fetchall()
            print(f"Found {len(rows)} PLTR trades.")
            for sym, payload_str in rows:
                data = json.loads(payload_str)
                if "2026-08-06" in data.get("entry_at", "") or "vwap_reversion" == data.get("strategy", ""):
                    print(json.dumps(data, indent=2))
        conn.close()
    except Exception as e:
        print(f"Error reading {p}: {e}")
