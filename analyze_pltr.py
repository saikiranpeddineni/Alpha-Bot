import sqlite3
import json

conn = sqlite3.connect('data/alphadesk.db')
cur = conn.cursor()

# Get shadow ledger events
cur.execute("SELECT payload FROM shadow_ledger WHERE symbol='PLTR'")
shadow = cur.fetchall()
print("\n--- Shadow Ledger (PLTR) ---")
for s in shadow:
    if '2026-08-06' in s[0]:
        try:
            data = json.loads(s[0])
            if "14:1" in data.get("timestamp", "") or "14:1" in data.get("event_time", "") or "14:1" in s[0]:
                print(json.dumps(data, indent=2))
        except:
            print(s[0])
            
cur.execute("SELECT payload FROM orders WHERE payload LIKE '%PLTR%' AND payload LIKE '%2026-08-06%'")
orders = cur.fetchall()
print("\n--- Orders (PLTR) ---")
for o in orders:
    print(o[0])

conn.close()
