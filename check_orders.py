import sqlite3
import json

conn = sqlite3.connect(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
cur = conn.cursor()

cur.execute("SELECT * FROM orders")
rows = cur.fetchall()
print(f"Total orders: {len(rows)}")
for r in rows[-20:]:
    payload = json.loads(r[3]) if r[3] else {}
    print(f"Order: id={r[0]}, ts={r[1]}, mode={r[2]}, symbol={payload.get('symbol')}, side={payload.get('side')}, type={payload.get('order_type')}, price={payload.get('price')}, stop_loss={payload.get('stop_loss')}, take_profit={payload.get('take_profit')}, strat={payload.get('strategy')}")

conn.close()
