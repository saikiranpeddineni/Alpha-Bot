import sqlite3
import pandas as pd
import json

master_db_path = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"

def inspect_rejections():
    conn = sqlite3.connect(master_db_path)
    cur = conn.cursor()
    cur.execute("SELECT policy_id, symbol, strategy, payload_json FROM spawn_shadow_events WHERE ts LIKE '2026-08-03%' LIMIT 5")
    rows = cur.fetchall()
    for r in rows:
        print(f"\nPolicy: {r[0]} | Symbol: {r[1]} | Strategy: {r[2]}")
        p = json.loads(r[3])
        print(json.dumps(p, indent=2))
    conn.close()

if __name__ == "__main__":
    inspect_rejections()
