import json
import sqlite3
from pathlib import Path

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")

conn = sqlite3.connect(str(MASTER_DB))
cur = conn.cursor()
cur.execute("SELECT id, symbol, pnl, exit_at, mode, payload FROM trades WHERE symbol='NVDA' AND exit_at LIKE '2026-08-03%'")
rows = cur.fetchall()
for r in rows:
    print("TRADE:", r[0], r[1], r[2], r[3])
    p = json.loads(r[5]) if r[5] else {}
    print("PAYLOAD:", json.dumps(p, indent=2))

conn.close()
