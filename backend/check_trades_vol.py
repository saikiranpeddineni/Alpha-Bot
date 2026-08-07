import sqlite3, json, datetime
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
c.execute('SELECT payload, exit_at FROM trades WHERE exit_at >= ?', ((datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat(),))
rows = c.fetchall()
total_vol = 0
for row in rows:
    try:
        data = json.loads(row[0])
        # Look for missing fields
        qty = float(data.get('qty') or data.get('filled_qty') or 0.0)
        entry = float(data.get('entry_price') or data.get('filled_avg_price') or 0.0)
        vol = abs(qty * entry)
        total_vol += vol
    except Exception as e:
        pass
print(f"Total rows: {len(rows)}, Total 24h Volume: {total_vol}")
