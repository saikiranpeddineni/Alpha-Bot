import sqlite3, json, datetime
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
c.execute('SELECT payload, exit_at FROM trades WHERE exit_at >= ?', ((datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat(),))
rows = c.fetchall()
total_volume = 0.0
count = 0
for row in rows:
    try:
        trade_data = json.loads(row[0])
    except Exception:
        continue
    entry_price = trade_data.get("entry_price", 0.0)
    qty = trade_data.get("qty", 0.0)
    total_volume += abs(qty * entry_price)
    count += 1
    print(f"Trade: qty={qty}, entry={entry_price}, vol={abs(qty*entry_price)}, symbol={trade_data.get('symbol')}")
print(f"Total processed rows: {count}, Total Volume: {total_volume}")
